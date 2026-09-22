"""Public-release configuration wiring and discontinuity regression tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import original_bridge as bridge
from original_stations import StationStore
from original_station_scan import scan_centers
from radio_sources import SourceConfig
from test_original_bridge import FakeSource, FakeProcessor, FakeWallpaper, wait_for


class ReleaseIntegrationTests(unittest.TestCase):
    def test_discovery_exits_without_server_or_receiver(self):
        with patch('radio_sources.enumerate_devices', return_value=[]) as discover, \
             patch.object(bridge, 'BridgeServer') as server, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bridge.main(['--backend', 'soapy', '--device-args', 'driver=example', '--list-devices']), 0)
            self.assertEqual(json.loads(output.getvalue()), [])
            self.assertEqual(discover.call_args.args[0].device_args, 'driver=example')
            server.assert_not_called()

    def test_cli_device_configuration_reaches_server(self):
        with patch.object(bridge, 'BridgeServer', side_effect=OSError('test occupied')) as server, \
             patch.object(bridge, 'is_existing_bridge', return_value=False):
            with self.assertRaises(OSError):
                bridge.main(['--backend', 'soapy', '--device-args', 'driver=example', '--sample-rate', '2500000',
                             '--gain', '12', '--channel', '1', '--frequency', '2400000000', '--keep-wallpaper'])
            self.assertEqual(server.call_args.kwargs['frequency'], 2_400_000_000)
            self.assertEqual(server.call_args.kwargs['source_config'].sample_rate, 2_500_000)
            self.assertEqual(server.call_args.kwargs['source_config'].channel, 1)

    def test_bad_cli_combinations_fail_before_hardware(self):
        with patch.object(bridge, 'BridgeServer') as server, contextlib.redirect_stderr(io.StringIO()):
            for args in (['--demo', '--auto-start'], ['--frequency', '6000000001'],
                         ['--sample-rate', '0'], ['--backend', 'rtl', '--device-args', 'driver=bad']):
                with self.assertRaises(SystemExit):
                    bridge.main(args)
            server.assert_not_called()

    def test_preferences_outside_checkout_and_microwave_frequency_memory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'INVISIBLE_TERRAIN_DATA_DIR': directory}):
            store = bridge.create_station_store()
            store.remember(frequency_hz=2_400_000_000)
            self.assertTrue((Path(directory) / 'radio_stations.json').is_file())
            self.assertEqual(bridge.create_station_store().snapshot()['last_frequency_hz'], 2_400_000_000)

    def test_scan_plan_covers_station_centers_at_other_rates(self):
        for rate in (1_800_000, 2_000_000, 2_048_000, 2_500_000, 6_000_000, 10_000_000):
            for station in range(87_500_000, 108_000_001, 100_000):
                self.assertTrue(any(abs(station - center) + 90_000 < rate * .45
                                    and abs(station - center) > 12_000 for center in scan_centers(rate)), (rate, station))

    def test_actual_rate_and_gap_reset_reach_controller(self):
        calls = []
        class GapSource(FakeSource):
            def __init__(self, frequency, demo):
                super().__init__(frequency, demo, calls)
                self.sample_rate = 2_500_000
                self.discontinuities = 0
                self.reads = 0
                self.warnings = ['Manual gain recommended']
            def read_iq(self, count):
                self.reads += 1
                if self.reads == 4:
                    self.discontinuities += 1
                return super().read_iq(count)
        controller = bridge.ReceiverController(frequency=100_000_000, station_store=StationStore(),
                       source_factory=GapSource, processor_factory=FakeProcessor, wallpaper_factory=FakeWallpaper,
                       source_config=SourceConfig(backend='soapy', device_args='driver=fake'))
        try:
            controller.request({'action': 'start'})
            wait_for(lambda: controller.snapshot()['generation'] >= 2 and controller.snapshot()['frame'] is not None)
            state = controller.snapshot()
            self.assertEqual(state['receiver']['sample_rate_hz'], 2_500_000)
            self.assertEqual(state['receiver']['warnings'], ['Manual gain recommended'])
            self.assertEqual(state['frame']['sample_rate_hz'], 2_500_000)
            self.assertFalse(state['audio']['enabled'])
            self.assertEqual(state['receiver']['backend'], 'soapy')
        finally:
            controller.shutdown(restore_wallpaper=False)


if __name__ == '__main__':
    unittest.main()
