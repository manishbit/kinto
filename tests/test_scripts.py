import unittest
from unittest import mock

from kinto import scripts
from kinto.core.storage.exceptions import ObjectNotFoundError


class DeleteCollectionTest(unittest.TestCase):
    def setUp(self):
        self.registry = mock.MagicMock()

    def test_delete_collection_in_read_only_display_an_error(self):
        with mock.patch("kinto.scripts.logger") as mocked:
            self.registry.settings = {"readonly": "true"}
            code = scripts.delete_collection(
                {"registry": self.registry}, "test_bucket", "test_collection"
            )
            assert code == 31
            mocked.error.assert_any_call("Cannot delete the collection while " "in readonly mode.")

    def test_delete_collection_remove_collection_objects(self):
        self.registry.storage.delete_all.return_value = [{"id": "1234"}, {"id": "5678"}]

        with mock.patch("kinto.scripts.logger") as mocked:
            scripts.delete_collection(
                {"registry": self.registry}, "test_bucket", "test_collection"
            )

        self.registry.storage.delete_all.assert_called_with(
            resource_name="record",
            parent_id="/buckets/test_bucket/collections/test_collection",
            with_deleted=False,
        )
        self.registry.storage.delete.assert_called_with(
            resource_name="collection",
            parent_id="/buckets/test_bucket",
            object_id="test_collection",
            with_deleted=False,
        )
        self.registry.permission.delete_object_permissions.assert_called_with(
            "/buckets/test_bucket/collections/test_collection",
            "/buckets/test_bucket/collections/test_collection/records/1234",
            "/buckets/test_bucket/collections/test_collection/records/5678",
        )

        mocked.info.assert_any_call("2 record(s) were deleted.")
        mocked.info.assert_any_call(
            "'/buckets/test_bucket/collections/test_collection' " "collection object was deleted."
        )

    def test_delete_collection_tell_when_no_objects_where_found(self):
        self.registry.storage.delete_all.return_value = []

        with mock.patch("kinto.scripts.logger") as mocked:
            scripts.delete_collection(
                {"registry": self.registry}, "test_bucket", "test_collection"
            )

        mocked.info.assert_any_call(
            "No records found for " "'/buckets/test_bucket/collections/test_collection'."
        )
        mocked.info.assert_any_call(
            "'/buckets/test_bucket/collections/test_collection' " "collection object was deleted."
        )
        mocked.info.assert_any_call("Related permissions were deleted.")

    def test_delete_collection_raise_if_the_bucket_does_not_exist(self):
        self.registry.storage.get.side_effect = ObjectNotFoundError
        with mock.patch("kinto.scripts.logger") as mocked:
            resp = scripts.delete_collection(
                {"registry": self.registry}, "test_bucket", "test_collection"
            )
        assert resp == 32
        mocked.error.assert_called_with("Bucket '/buckets/test_bucket' does not exist.")

    def test_delete_collection_raise_if_the_collection_does_not_exist(self):
        self.registry.storage.get.side_effect = ["", ObjectNotFoundError]
        with mock.patch("kinto.scripts.logger") as mocked:
            resp = scripts.delete_collection(
                {"registry": self.registry}, "test_bucket", "test_collection"
            )
        assert resp == 33
        mocked.error.assert_called_with(
            "Collection '/buckets/test_bucket/collections/test_collection' " "does not exist."
        )


class RebuildQuotasTest(unittest.TestCase):
    def setUp(self):
        self.registry = mock.MagicMock()
        self.registry.settings = {"includes": "kinto.plugins.quotas"}

    def test_rebuild_quotas_in_read_only_display_an_error(self):
        with mock.patch("kinto.scripts.logger") as mocked:
            self.registry.settings["readonly"] = "true"
            code = scripts.rebuild_quotas({"registry": self.registry})
            assert code == 41
            mocked.error.assert_any_call("Cannot rebuild quotas while " "in readonly mode.")

    def test_rebuild_quotas_when_not_included_display_an_error(self):
        with mock.patch("kinto.scripts.logger") as mocked:
            self.registry.settings["includes"] = ""
            code = scripts.rebuild_quotas({"registry": self.registry})
            assert code == 42
            mocked.error.assert_any_call(
                "Cannot rebuild quotas when " "quotas plugin is not installed."
            )

    def test_rebuild_quotas_calls_quotas_script(self):
        with mock.patch("kinto.scripts.quotas.rebuild_quotas") as mocked:
            code = scripts.rebuild_quotas({"registry": self.registry})
            assert code == 0
            mocked.assert_called_with(self.registry.storage, dry_run=False)
