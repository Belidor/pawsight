"""PAWSIGHT_CLASSES end to end, in a child process.

The class list is computed when pawsight.labels is imported and copied into
every module by name, so switching it inside this process would test a
half-switched state. Each case runs a fresh interpreter with its own
environment, the way the detector and the brain start.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]

try:
    import cv2  # noqa: F401
    HAVE_CV2 = True
except ImportError:          # brain image
    HAVE_CV2 = False


def run(code: str, classes: str | None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PAWSIGHT_CLASSES"}
    if classes is not None:
        env["PAWSIGHT_CLASSES"] = classes
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env,
                          cwd=cwd or REPO, capture_output=True, text=True, timeout=60)


class SharedPreset(unittest.TestCase):
    def ok(self, code, classes):
        r = run(code, classes)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_base_three_by_default(self):
        out = self.ok("from pawsight.labels import DATASET_CLASSES as D; print(D)", "")
        self.assertEqual(out.strip(), "{0: 'dog', 1: 'bowl', 2: 'person'}")

    def test_shared_keeps_base_indices_and_appends(self):
        out = self.ok("""
            from pawsight.labels import DATASET_CLASSES as D, EXTRA_CLASSES as X, COCO_TO_DATASET as C
            print(D); print(X); print(sorted(C.items()))
        """, "shared")
        d, x, c = out.strip().splitlines()
        self.assertEqual(d, "{0: 'dog', 1: 'bowl', 2: 'person', 3: 'couch', 4: 'bed', "
                            "5: 'chair', 6: 'dining table', 7: 'rug'}")
        self.assertIn("'rug'", x)
        # COCO couch 57, chair 56, bed 59, dining table 60; no rug in COCO
        self.assertEqual(c, "[(0, 2), (16, 0), (45, 1), (56, 5), (57, 3), (59, 4), (60, 6)]")

    def test_label_validation_follows_the_list(self):
        code = """
            from pawsight.labelcheck import validate_boxes, to_yolo
            b = [{"cls": "rug", "cx": .5, "cy": .8, "w": .4, "h": .1},
                 {"cls": "dining table", "cx": .3, "cy": .5, "w": .2, "h": .2}]
            ok, err = validate_boxes(b); print(ok, err)
            if ok: print(to_yolo(b).split()[0], to_yolo(b).splitlines()[1].split()[0])
        """
        self.assertEqual(self.ok(code, "shared").split(), ["True", "None", "7", "6"])
        # without the preset the same boxes are refused, not written with a wrong id
        self.assertIn("unknown class: 'rug'", self.ok(code, ""))

    def test_bad_value_stops_the_process(self):
        r = run("import pawsight.labels", "dog,person,bowl,rug")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must start with dog,bowl,person", r.stderr)

    def test_dashboard_classes_endpoint(self):
        out = self.ok("""
            import asyncio, json, tempfile
            from pathlib import Path
            from aiohttp.test_utils import TestClient, TestServer
            from pawsight.config import Config
            from pawsight.web import Dashboard
            async def main():
                d = Path(tempfile.mkdtemp())
                c = TestClient(TestServer(Dashboard(Config(db_path=d / "e.db", snap_dir=d / "s")).app()))
                await c.start_server()
                print(json.dumps(await (await c.get("/api/label/classes")).json()))
                await c.close()
            asyncio.run(main())
        """, "shared")
        import json
        d = json.loads(out)
        self.assertEqual(d["classes"][:3], ["dog", "bowl", "person"])
        self.assertEqual(d["extra"], ["couch", "bed", "chair", "dining table", "rug"])

    @unittest.skipUnless(HAVE_CV2, "dataset.py needs OpenCV")
    def test_relabel_reaches_training_with_extra_classes(self):
        """A rug drawn on the page: written as 7, read back as rug, copied into
        the training split, named in data.yaml, and the review counters see it."""
        with TemporaryDirectory() as tmp:
            out = self.ok(f"""
                from pathlib import Path
                import yaml
                from pawsight import review, train
                from pawsight.dataset import Capture
                root = Path({tmp!r}) / "dataset"
                cap = Capture(root)
                for i in range(60):
                    (root / "images" / f"dog_{{i:03d}}.jpg").write_bytes(b"jpg")
                    (root / "labels" / f"dog_{{i:03d}}.txt").write_text("0 0.5 0.5 0.2 0.2\\n")
                cap.relabel("dog_000", [{{"cls": "dog", "cx": .5, "cy": .5, "w": .2, "h": .2}},
                                        {{"cls": "rug", "cx": .5, "cy": .9, "w": .6, "h": .1}}])
                print((root / "labels" / "dog_000.txt").read_text().splitlines()[1].split()[0])
                print([b["cls"] for b in review.read_label(root / "labels" / "dog_000.txt")])
                print(review.listing(root)["counts"]["no_extra"])
                train._split(root)
                split = "val" if (root / "labels" / "val" / "dog_000.txt").exists() else "train"
                print((root / "labels" / split / "dog_000.txt").read_text().splitlines()[1].split()[0])
                print(yaml.safe_load(train._write_data_yaml(root).read_text())["names"][7])
            """, "shared")
        self.assertEqual(out.strip().splitlines(), ["7", "['dog', 'rug']", "59", "7", "rug"])

    def test_env_file_is_read_when_env_is_unset(self):
        """labels.py can be imported before config.py loads .env: it reads the
        file itself, and the process environment still wins."""
        with TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "pawsight"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "labels.py").write_text((REPO / "pawsight" / "labels.py").read_text())
            (Path(tmp) / ".env").write_text(
                "# comment\nPAWSIGHT_MODEL=x.pt\nPAWSIGHT_CLASSES='dog,bowl,person,rug'  # mine\n")
            code = "from pawsight.labels import CLASS_NAMES; print(CLASS_NAMES)"
            env = {k: v for k, v in os.environ.items() if k != "PAWSIGHT_CLASSES"}
            env["PYTHONPATH"] = tmp
            r = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.strip(), "('dog', 'bowl', 'person', 'rug')", r.stderr)
            env["PAWSIGHT_CLASSES"] = ""
            r = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.strip(), "('dog', 'bowl', 'person')", r.stderr)


if __name__ == "__main__":
    unittest.main()
