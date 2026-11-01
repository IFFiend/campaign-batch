import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("campaign",ROOT/"scripts/campaign.py")
campaign=importlib.util.module_from_spec(spec)
spec.loader.exec_module(campaign)

class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def write(self, data, name="input.json"):
        path = self.work / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def events(self):
        return json.loads((ROOT / "assets/demo.json").read_text())

    def test_all_campaign_treatments_and_sizes_export_and_retain_facts(self):
        data = self.events()
        report = campaign.build(self.write(data), self.work / "out")
        self.assertEqual((report["expected"], report["rendered"], report["failed"]), (9,9,[]))
        for event in data["events"]:
            for label, size in campaign.SIZES.items():
                stem = f"{event['id']}.{event['locale']}.{label}"
                with Image.open(self.work / "out" / f"{stem}.png") as image:
                    self.assertEqual(image.size, size)
                    self.assertGreater(len(image.getcolors(2**24)), 100)
                root = ET.parse(self.work / "out" / f"{stem}.svg").getroot()
                text = " ".join(root.itertext())
                for key in ("date", "time", "location", "cta"):
                    self.assertIn(event[key], text)
        self.assertEqual(report["visual_review"], "pending")

    def test_campaign_duplicate_keys_stop_before_output(self):
        data = self.events()
        data["events"].append(copy.deepcopy(data["events"][0]))
        with self.assertRaises(campaign.InputError):
            campaign.build(self.write(data), self.work / "out")
        self.assertFalse((self.work / "out").exists())

    def test_campaign_short_labels_cannot_silently_drop_location(self):
        data = self.events()
        data["events"][0]["labels"] = ["DATE"]
        with self.assertRaises(campaign.InputError):
            campaign.build(self.write(data), self.work / "out")

    def test_layout_families_have_different_geometry_and_bounded_visuals(self):
        data = campaign.validate(self.events(),self.work)
        event = copy.deepcopy(data["events"][0])
        event["title"] = "Better ideas."
        boxes = {}
        for family in sorted(campaign.FAMILIES):
            event["family"] = family
            for size in campaign.SIZES:
                scene = campaign.compose(data,event,size)
                title = next(e for e in scene.elements if e.get("role")=="title")
                boxes[family,size] = title["box"]
                for e in scene.elements:
                    x,y,r,b = e["box"]
                    self.assertTrue(0 <= x <= r <= scene.w and 0 <= y <= b <= scene.h)
        for size in campaign.SIZES:
            self.assertEqual(len({tuple(boxes[f,size]) for f in campaign.FAMILIES}),3)

    def test_campaign_blocks_nonexistent_daylight_saving_time(self):
        data = self.events()
        data["events"][0].update(date="2026-10-04",time="02:30 AEST")
        with self.assertRaisesRegex(campaign.InputError,"does not exist"):
            campaign.build(self.write(data),self.work/"out")

    def test_campaign_invalid_date_and_timezone_rejected(self):
        for key, bad in [("date", "2026-02-30"), ("time", "18:30 AEST"), ("timezone", "Made/Up"), ("id", "../escape")]:
            with self.subTest(key=key):
                data = self.events()
                data["events"][0][key] = bad
                with self.assertRaises(campaign.InputError):
                    campaign.build(self.write(data), self.work / key)

    def test_campaign_overlong_copy_reports_missing_exports(self):
        data = self.events()
        data["events"][0]["title"] = "Unreasonably long headline " * 100
        report = campaign.build(self.write(data), self.work / "out", ["square"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual((report["rendered"],len(report["failed"])),(2,1))
        self.assertFalse((self.work / "out/build.en.square.png").exists())

    def test_campaign_no_silent_glyph_substitution(self):
        data = self.events()
        data["events"] = data["events"][:1]
        data["events"][0]["title"] = "Missing \U0010ffff"
        report = campaign.build(self.write(data), self.work / "out", ["square"])
        self.assertEqual(report["rendered"],0)
        self.assertIn("lacks glyphs",report["failed"][0]["error"])

    @unittest.skipUnless(Path('/System/Library/Fonts/Hiragino Sans GB.ttc').is_file(), 'Requires a supplied CJK font; not bundled')
    def test_supplied_cjk_font_exports_three_layouts_without_losing_copy(self):
        data = self.events()
        data['brand'].update(font_regular='/System/Library/Fonts/Hiragino Sans GB.ttc',font_bold='/System/Library/Fonts/Hiragino Sans GB.ttc')
        for event in data['events']:
            event.update(title='一起做事，\n也一起相聚。',locale='zh-CN',location='悉尼')
        result = campaign.build(self.write(data),self.work/'out',['square'])
        self.assertEqual((result['rendered'],result['failed']),(3,[]))
        for event in data['events']:
            root=ET.parse(self.work/'out'/f"{event['id']}.zh-CN.square.svg").getroot()
            self.assertIn('一起做事，', ' '.join(root.itertext()))
            self.assertIn('悉尼', ' '.join(root.itertext()))

    def test_campaign_low_contrast_is_visible_warning(self):
        data = self.events()
        data["brand"].update(background="#ffffff", ink="#eeeeee")
        data["events"] = data["events"][:1]
        report = campaign.build(self.write(data),self.work/"out",["square"])
        self.assertEqual(report["status"], "review_required")
        self.assertTrue(any(i["check"] == "contrast" for i in report["items"][0]["issues"]))

    def test_protected_crop_keeps_whole_supplied_subject(self):
        image = self.work/"subject.png"
        Image.new("RGB",(800,200),"red").save(image)
        data = campaign.validate(self.events(),self.work)
        scene = campaign.Scene(300,300,"#ffffff",data["brand"])
        scene.image("hero",image,[0,0,200,300],focus=[1,.5],protect=[0,0,.2,1])
        el = scene.elements[-1]
        self.assertEqual(el["mode"],"contain")
        self.assertEqual(el["crop"],[0,0,800,200])
        self.assertEqual(el["box"][2]-el["box"][0],200)

    def test_campaign_svg_escapes_untrusted_copy(self):
        data = self.events()
        data["events"] = data["events"][:1]
        data["events"][0]["title"] = '<script> & "'
        report = campaign.build(self.write(data),self.work/"out",["square"])
        self.assertEqual(report["rendered"],1)
        root = ET.parse(self.work/"out/build.en.square.svg").getroot()
        self.assertFalse(any(e.tag.endswith("script") for e in root.iter()))

    def test_campaign_will_not_mix_old_and_new_exports(self):
        output = self.work/"out"
        output.mkdir()
        (output/"old.png").write_text("old")
        with self.assertRaises(campaign.InputError):
            campaign.build(self.write(self.events()), output)
        self.assertEqual((output/"old.png").read_text(),"old")

    def test_nonobject_input_is_rejected(self):
        with self.assertRaises(campaign.InputError):
            campaign.build(self.write([]),self.work/"out")

if __name__=="__main__":
    unittest.main()
