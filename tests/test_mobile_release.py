import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "deploy-mobile-apps"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from mobile_release_lib import BatchStore, load_inventory, new_batch


class InventoryTests(unittest.TestCase):
    def test_inventory_defines_the_three_managed_apps(self):
        inventory = load_inventory(SKILL_ROOT / "references/apps.json")

        self.assertEqual(set(inventory.apps), {"pocket-manage", "installers", "partner"})
        for app in inventory.apps.values():
            self.assertEqual(app.dev_branch, "dev")
            self.assertEqual(app.release_branch, "release")
            self.assertEqual(app.submodule_path, "packages")
            self.assertEqual(app.submodule_branch, "main")

    def test_batch_store_round_trips_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BatchStore(Path(directory))
            batch = new_batch(["pocket-manage"], now="2026-08-14T12:00:00Z")

            store.save(batch)

            self.assertEqual(store.load(batch["batch_id"]), batch)
            self.assertNotIn("token", json.dumps(batch).lower())
