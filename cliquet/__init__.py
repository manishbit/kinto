"""Main entry point
"""
import datetime
import json
import warnings

from dateutil import parser as dateparser
import pkg_resources
import logging

from pyramid.events import NewRequest, NewResponse
from pyramid.httpexceptions import HTTPTemporaryRedirect, HTTPGone
from pyramid_multiauth import MultiAuthenticationPolicy
from pyramid.security import NO_PERMISSION_REQUIRED

from cliquet import authentication
from cliquet import errors
from cliquet import utils
from cliquet.session import SessionCache

from cornice import Service


# Module version, as defined in PEP-0396.
__version__ = pkg_resources.get_distribution(__package__).version

# The API version is derivated from the module version.
API_VERSION = 'v%s' % __version__.split('.')[0]

# Main cliquet logger
logger = logging.getLogger(__name__)


DEFAULT_SETTINGS = {
    'fxa-oauth.cache_ttl_seconds': 5 * 60,
    'fxa-oauth.client_id': None,
    'fxa-oauth.client_secret': None,
    'fxa-oauth.oauth_uri': None,
    'fxa-oauth.scope': 'profile',
    'cliquet.backoff': None,
    'cliquet.basic_auth_enabled': False,
    'cliquet.batch_max_requests': 25,
    'cliquet.delete_collection_enabled': True,
    'cliquet.eos': None,
    'cliquet.eos_message': None,
    'cliquet.eos_url': None,
    'cliquet.http_scheme': None,
    'cliquet.paginate_by': None,
    'cliquet.project_docs': '',
    'cliquet.project_name': '',
    'cliquet.project_version': '',
    'cliquet.retry_after_seconds': 30,
    'cliquet.session_backend': 'cliquet.session.redis',
    'cliquet.session_url': '',
    'cliquet.storage_backend': 'cliquet.storage.redis',
    'cliquet.storage_url': '',
    'cliquet.storage_max_fetch_size': 10000,
    'cliquet.userid_hmac_secret': '',
}


def load_default_settings(config):
    """Read settings provided in Paste ini file, set default values and
    replace if defined as environment variable.
    """
    settings = config.get_settings()
    for key, value in DEFAULT_SETTINGS.items():
        settings.setdefault(key, utils.read_env(key, value))
    config.add_settings(settings)


def handle_api_redirection(config):
    """Add a view which redirects to the current version of the API.
    """
    # Disable the route prefix passed by the app.
    route_prefix = config.route_prefix
    config.route_prefix = None

    def _redirect_to_version_view(request):
        raise HTTPTemporaryRedirect(
            '/%s/%s' % (route_prefix, request.matchdict['path']))

    # Redirect to the current version of the API if the prefix isn't used.
    config.add_route(name='redirect_to_version',
                     pattern='/{path:(?!%s).*}' % route_prefix)

    config.add_view(view=_redirect_to_version_view,
                    route_name='redirect_to_version',
                    permission=NO_PERMISSION_REQUIRED)

    config.route_prefix = route_prefix


def set_auth(config):
    """Define the authentication and authorization policies.
    """
    settings = config.registry.settings
    oauth_cache_ttl = int(settings['fxa-oauth.cache_ttl_seconds'])

    policies = [
        authentication.Oauth2AuthenticationPolicy(
            cache=SessionCache(config.registry.session, ttl=oauth_cache_ttl)),
        authentication.BasicAuthAuthenticationPolicy(),
    ]
    authn_policy = MultiAuthenticationPolicy(policies)
    authz_policy = authentication.AuthorizationPolicy()

    config.set_authorization_policy(authz_policy)
    config.set_authentication_policy(authn_policy)
    # XXX: Should be restore as soon as Cornice is fixed (see #273)
    # config.set_default_permission('readwrite')


def attach_http_objects(config):
    """Attach HTTP requests/responses objects.

    This is useful to attach objects to the request object for easier
    access, and to pre-process responses.
    """

    def on_new_request(event):
        # Save the time the request was received by the server.
        event.request._received_at = utils.msec_time()

        # Attach objects on requests for easier access.
        event.request.db = config.registry.storage

        http_scheme = config.registry.settings['cliquet.http_scheme']
        if http_scheme:
            event.request.scheme = http_scheme

    config.add_subscriber(on_new_request, NewRequest)

    def on_new_response(event):
        if hasattr(event.request, '_received_at'):
            duration = (utils.msec_time() - event.request._received_at)
        else:
            duration = "unknown"

        # Display the status of the request as well as the time spent
        # on the server.
        pattern = '"{method} {path}" {status} {size} ({duration} ms)'
        msg = pattern.format(
            method=event.request.method,
            path=event.request.path,
            status=event.response.status_code,
            size=event.response.content_length,
            duration=duration)
        logger.debug(msg)

        # Add backoff in response headers.
        backoff = '%s' % config.registry.settings['cliquet.backoff']
        if backoff is not None:
            event.request.response.headers['Backoff'] = backoff.encode('utf-8')

    config.add_subscriber(on_new_response, NewResponse)


def end_of_life_tween_factory(handler, registry):
    """Pyramid tween to handle service end of life."""

    deprecated_response = errors.http_error(
        HTTPGone(),
        errno=errors.ERRORS.SERVICE_DEPRECATED,
        message="The service you are trying to connect no longer exists "
                "at this location.")

    def eos_tween(request):
        eos_date = registry.settings['cliquet.eos']
        eos_url = registry.settings['cliquet.eos_url']
        eos_message = registry.settings['cliquet.eos_message']
        if eos_date:
            eos_date = dateparser.parse(eos_date)
            alert = {}
            if eos_url is not None:
                alert['url'] = eos_url

            if eos_message is not None:
                alert['message'] = eos_message

            if eos_date > datetime.datetime.now():
                alert['code'] = "soft-eol"
                response = handler(request)
            else:
                response = deprecated_response
                alert['code'] = "hard-eol"
            response.headers['Alert'] = json.dumps(alert)
            return response
        return handler(request)
    return eos_tween


def includeme(config):
    # Monkey Patch Cornice Service to setup the global CORS configuration.
    Service.cors_origins = ('*',)
    Service.default_cors_headers = ('Backoff', 'Retry-After', 'Alert')

    load_default_settings(config)
    settings = config.get_settings()

    handle_api_redirection(config)
    config.add_tween("cliquet.end_of_life_tween_factory")

    storage = config.maybe_dotted(settings['cliquet.storage_backend'])
    config.registry.storage = storage.load_from_config(config)

    session = config.maybe_dotted(settings['cliquet.session_backend'])
    config.registry.session = session.load_from_config(config)

    set_auth(config)
    attach_http_objects(config)

    # Include cornice and discover views.
    config.include("cornice")
    config.scan("cliquet.views")


def initialize_cliquet(config, version=None, project_name=None):
    """Initialize Cliquet with the given configuration, version and project
    name.

    This will basically include cliquet in Pyramid and set route prefix based
    on the specified version.

    :param config: Pyramid configuration
    :type config: pyramid.config.Configurator
    :param version: Current project version (e.g. '0.0.1') if not defined
        in application settings.
    :type version: string
    :param project_name: Project name if not defined in application settings.
    :type project_name: string
    """
    settings = config.registry.settings

    # The API version is derivated from the module version.
    project_version = settings.get('cliquet.project_version') or version
    settings['cliquet.project_version'] = project_version
    try:
        api_version = 'v%s' % project_version.split('.')[0]
    except (AttributeError, ValueError):
        raise ValueError('Invalid project version')

    project_name = settings.get('cliquet.project_name') or project_name
    settings['cliquet.project_name'] = project_name
    if not project_name:
        warnings.warn('No value specified for `project_name`')

    # Include cliquet views with the correct api version prefix.
    config.include("cliquet", route_prefix=api_version)
    config.route_prefix = api_version
