import unittest

from camera_controls import ControlError, create_app, parse_controls, validate_value


SAMPLE_CONTROLS = """
User Controls

                     brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=-8193 value=0 flags=has-min-max
        white_balance_automatic 0x0098090c (bool)   : default=1 value=1
           power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=1 value=1 (50 Hz)
                                0: Disabled
                                1: 50 Hz
                                2: 60 Hz
      white_balance_temperature 0x0098091a (int)    : min=2800 max=6500 step=1 default=57343 value=4600 flags=inactive, has-min-max
"""


class ParserTests(unittest.TestCase):
    def test_parses_real_control_types_ranges_menus_and_flags(self):
        controls = parse_controls(SAMPLE_CONTROLS)

        self.assertEqual([item["name"] for item in controls], [
            "brightness",
            "white_balance_automatic",
            "power_line_frequency",
            "white_balance_temperature",
        ])
        self.assertEqual(controls[0]["min"], -64)
        self.assertEqual(controls[0]["step"], 1)
        self.assertFalse(controls[0]["default_valid"])
        self.assertEqual(controls[1]["type"], "bool")
        self.assertTrue(controls[1]["default_valid"])
        self.assertEqual(controls[2]["menu"], {0: "Disabled", 1: "50 Hz", 2: "60 Hz"})
        self.assertTrue(controls[3]["inactive"])
        self.assertFalse(controls[3]["default_valid"])

    def test_rejects_values_outside_range_or_menu(self):
        controls = {item["name"]: item for item in parse_controls(SAMPLE_CONTROLS)}

        with self.assertRaises(ControlError):
            validate_value(controls["brightness"], 65)
        with self.assertRaises(ControlError):
            validate_value(controls["power_line_frequency"], 3)
        self.assertEqual(validate_value(controls["white_balance_automatic"], True), 1)


class FakeCamera:
    device = "/dev/v4l/by-id/test-camera-video-index0"

    def __init__(self):
        self.controls = parse_controls(SAMPLE_CONTROLS)
        self.set_calls = []

    def list_controls(self):
        return self.controls

    def set_control(self, name, value):
        self.set_calls.append((name, value))
        for control in self.controls:
            if control["name"] == name:
                control["value"] = value
                return
        raise ControlError("Unknown control")

    def reset_defaults(self):
        applied = []
        skipped = []
        for control in self.controls:
            if control["default_valid"]:
                control["value"] = control["default"]
                applied.append(control["name"])
            else:
                skipped.append(control["name"])
        return {"applied": applied, "skipped": skipped, "errors": []}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.app = create_app(
            camera=self.camera,
            stream_url="http://gravipi.local:8080/stream",
            allowed_networks=["127.0.0.0/8", "192.168.0.0/16"],
        )
        self.client = self.app.test_client()

    def test_index_contains_existing_stream_without_opening_camera(self):
        response = self.client.get("/", environ_base={"REMOTE_ADDR": "192.168.1.10"})

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"http://gravipi.local:8080/stream", response.data)

    def test_api_reads_current_controls_and_device(self):
        response = self.client.get("/api/controls", environ_base={"REMOTE_ADDR": "192.168.1.10"})

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["device"], self.camera.device)
        self.assertEqual(body["controls"][0]["value"], 0)

    def test_setting_control_validates_and_returns_refreshed_state(self):
        response = self.client.put(
            "/api/controls/brightness",
            json={"value": 12},
            environ_base={"REMOTE_ADDR": "192.168.1.10"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.camera.set_calls, [("brightness", 12)])
        self.assertEqual(response.get_json()["controls"][0]["value"], 12)

    def test_rejects_unknown_control_and_non_lan_client(self):
        bad_control = self.client.put(
            "/api/controls/not_real",
            json={"value": 1},
            environ_base={"REMOTE_ADDR": "192.168.1.10"},
        )
        outside_lan = self.client.get("/api/controls", environ_base={"REMOTE_ADDR": "8.8.8.8"})

        self.assertEqual(bad_control.status_code, 400)
        self.assertEqual(outside_lan.status_code, 403)

    def test_reset_reports_invalid_camera_defaults_as_skipped(self):
        response = self.client.post(
            "/api/reset",
            headers={"X-Requested-With": "camera-controls"},
            environ_base={"REMOTE_ADDR": "192.168.1.10"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("brightness", body["reset"]["skipped"])
        self.assertIn("white_balance_automatic", body["reset"]["applied"])


class CsrfAndHostTests(unittest.TestCase):
    LAN = {"REMOTE_ADDR": "192.168.1.10"}

    def setUp(self):
        self.camera = FakeCamera()
        self.reset_calls = 0
        original_reset = self.camera.reset_defaults

        def counting_reset():
            self.reset_calls += 1
            return original_reset()

        self.camera.reset_defaults = counting_reset
        self.app = create_app(
            camera=self.camera,
            stream_url="http://gravipi.local:8080/stream",
            allowed_networks=["127.0.0.0/8", "192.168.0.0/16"],
        )
        self.client = self.app.test_client()

    def test_reset_with_foreign_origin_is_rejected(self):
        response = self.client.post(
            "/api/reset",
            headers={"Origin": "http://evil.example", "X-Requested-With": "camera-controls"},
            environ_base=self.LAN,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reset_calls, 0)

    def test_reset_without_origin_and_marker_header_is_rejected(self):
        response = self.client.post("/api/reset", environ_base=self.LAN)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reset_calls, 0)

    def test_reset_as_cross_site_form_post_is_rejected(self):
        response = self.client.post(
            "/api/reset",
            data={"a": "b"},
            headers={"Origin": "http://evil.example"},
            environ_base=self.LAN,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reset_calls, 0)

    def test_put_with_foreign_origin_is_rejected(self):
        response = self.client.put(
            "/api/controls/brightness",
            json={"value": 5},
            headers={"Origin": "http://evil.example"},
            environ_base=self.LAN,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.camera.set_calls, [])

    def test_foreign_host_header_is_rejected(self):
        for path in ("/", "/api/controls"):
            response = self.client.get(
                path, headers={"Host": "evil.example"}, environ_base=self.LAN
            )
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.get_json()["error"], "Unexpected Host header")

        rebound = self.client.post(
            "/api/reset",
            headers={
                "Host": "evil.example:8081",
                "Origin": "http://evil.example:8081",
                "X-Requested-With": "camera-controls",
            },
            environ_base=self.LAN,
        )
        self.assertEqual(rebound.status_code, 403)
        self.assertEqual(self.reset_calls, 0)

    def test_legitimate_requests_are_accepted(self):
        reset = self.client.post(
            "/api/reset",
            headers={
                "Host": "gravipi.local:8081",
                "Origin": "http://gravipi.local:8081",
                "X-Requested-With": "camera-controls",
            },
            environ_base=self.LAN,
        )
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(self.reset_calls, 1)

        by_ip = self.client.put(
            "/api/controls/brightness",
            json={"value": 3},
            headers={
                "Host": "192.168.1.50:8081",
                "Origin": "http://192.168.1.50:8081",
                "X-Requested-With": "camera-controls",
            },
            environ_base=self.LAN,
        )
        self.assertEqual(by_ip.status_code, 200)

        json_without_origin = self.client.put(
            "/api/controls/brightness",
            json={"value": 4},
            headers={"Host": "localhost:8081"},
            environ_base=self.LAN,
        )
        self.assertEqual(json_without_origin.status_code, 200)

        read = self.client.get(
            "/api/controls", headers={"Host": "gravipi.local:8081"}, environ_base=self.LAN
        )
        self.assertEqual(read.status_code, 200)

    def test_allowed_hosts_can_be_configured(self):
        app = create_app(
            camera=self.camera,
            stream_url="http://gravipi.local:8080/stream",
            allowed_networks=["192.168.0.0/16"],
            allowed_hosts=["camera.lan"],
        )
        client = app.test_client()

        ok = client.get("/api/controls", headers={"Host": "camera.lan:8081"}, environ_base=self.LAN)
        denied = client.get("/api/controls", headers={"Host": "gravipi.local"}, environ_base=self.LAN)

        self.assertEqual(ok.status_code, 200)
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
