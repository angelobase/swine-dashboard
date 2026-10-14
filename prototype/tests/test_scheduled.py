"""Tests for scheduled sessions. No camera, modem or broker needed:  python3 -m unittest discover -s tests -v"""
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
for name in ('serial', 'spidev', 'onnxruntime'):  # hardware libraries are stubbed when absent
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)

import capture_once as co  # noqa: E402
import gsm_sms  # noqa: E402
import inlayo8  # noqa: E402
import pig_ids  # noqa: E402
import screening as sc  # noqa: E402


class Screening(unittest.TestCase):
    def test_threshold_is_inclusive(self):
        self.assertEqual(sc.thermal_status(36.7, 36.7), 'Elevated')
        self.assertEqual(sc.thermal_status(36.69, 36.7), 'Normal')
        self.assertEqual(sc.thermal_status(None, 36.7), 'No result')

    def test_bout_counting_and_statuses(self):
        t = sc.new_track(0)
        rows = [sc.step(t, i, obs, lying, 37.0, 1800, 3, 36.7) for i, (obs, lying) in
                enumerate([(True, True), (True, True), (True, True), (False, None), (True, True)])]
        self.assertEqual([r['behavioral_status'] for r in rows], ['Normal', 'Normal', 'Abnormal', 'No data', 'Normal'])
        self.assertEqual([r['lying_s'] for r in rows], [1800, 3600, 5400, 0, 1800])  # a missed pig ends the bout
        self.assertEqual(rows[3]['posture'], 'Unknown')
        self.assertEqual(rows[3]['thermal_status'], 'No result')
        self.assertEqual(t['max_streak'], 3)

    def test_evaluation_needs_both_conditions_and_a_recent_temperature(self):
        def track(max_streak, temp, temp_seq):
            return {**sc.new_track(0), 'max_streak': max_streak, 'last_temp': temp, 'last_temp_seq': temp_seq}
        tracks = {'1': track(6, 37.0, 38), '2': track(5, 39.0, 38), '3': track(6, 36.0, 38),
                  '4': track(6, 38.0, 35), '5': track(6, 38.0, 36)}
        r = sc.evaluate(tracks, final_seq=38, interval_s=1800, rule_n=6, threshold_c=36.7, lookback=3)
        by = {p['pig_id']: p for p in r['pigs']}
        self.assertTrue(by[1]['alert'])
        self.assertFalse(by[2]['alert'])                   # lying bout too short
        self.assertFalse(by[3]['alert'])                   # final temperature below threshold
        self.assertIsNone(by[4]['final_temp_c'])           # seq 35 is outside the last 3 captures (36, 37, 38)
        self.assertEqual(by[4]['reason'], 'No recent valid temperature')
        self.assertTrue(by[5]['alert'])                    # seq 36 is inside
        self.assertEqual(r['flagged'], [1, 5])

    def test_sms_fits_one_message_and_is_gsm_safe(self):
        pigs = [{'pig_id': i, 'alert': True, 'final_temp_c': 36.7 + i / 10, 'longest_lying_s': 3600 * 3 + 1800} for i in range(1, 12)]
        msg = sc.build_sms('PEN-01', {'pigs': pigs}, time.time(), 68400, test=True)
        self.assertLessEqual(sc.septets(msg), 160)
        self.assertTrue(msg.startswith('[TEST] PIG ALERT PEN-01'))
        self.assertIn('more pigs', msg)
        self.assertIn('Pig 11: 37.8C, lying 3h30m', msg)  # hottest first
        gsm_sms.validate_message(msg)
        self.assertEqual((sc.fmt_hm(45), sc.fmt_hm(90), sc.fmt_hm(3 * 3600 + 1800)), ('45s', '2m', '3h30m'))


class Ids(unittest.TestCase):
    def test_nearby_keeps_id_far_gets_new(self):
        prev = [{'id': 1, 'box': [10, 10, 110, 60]}, {'id': 2, 'box': [400, 300, 500, 360]}]
        ids, nxt = pig_ids.assign(prev, [[402, 305, 498, 362], [15, 12, 112, 61]], 3, (640, 480))
        self.assertEqual((ids, nxt), ([2, 1], 3))
        ids, nxt = pig_ids.assign(prev, [[600, 10, 639, 40]], 3, (640, 480))
        self.assertEqual((ids, nxt), ([3], 4))


class CapturePure(unittest.TestCase):
    def test_cluster_majority_and_minimum_frames(self):
        lying = lambda n: n == 'laying'
        a, b = np.array([10, 10, 100, 60.]), np.array([300, 200, 400, 260.])
        frames = [[(a, .95, 1, 'laying'), (b, .95, 0, 'standing')], [(a + 2, .96, 1, 'laying')],
                  [(a + 1, .94, 0, 'standing'), (b + 1, .97, 0, 'standing')], [(a, .95, 1, 'laying')],
                  [(np.array([600, 400, 630, 470.]), .99, 0, 'standing')]]
        pigs = co.cluster_detections(frames, lying)
        self.assertEqual(len(pigs), 1)                    # b seen in 2 of 5 frames, the stray box in 1: dropped
        self.assertTrue(pigs[0]['lying'])                 # 3 of 4 votes
        self.assertEqual(pigs[0]['frames_seen'], 4)

    def test_mapping_and_temperature(self):
        self.assertEqual(co.map_box([320, 240, 640, 480], None, (640, 480)), [80, 60, 160, 120])
        h = np.array([[0.25, 0, 0], [0, 0.25, 0], [0, 0, 1.]])
        self.assertEqual(co.map_box([0, 0, 64, 48], h, (640, 480)), [0, 0, 16, 12])
        self.assertIsNone(co.map_box([700, 500, 900, 600], h, (640, 480)))
        celsius = np.full((120, 160), 24.0, dtype=np.float32)
        celsius[20:40, 20:60] = 37.0
        celsius[30:32, 30:32] = 45.0                      # a few hot pixels do not dominate
        self.assertAlmostEqual(co.pig_temperature(celsius, [10, 10, 70, 50]), 37.0, delta=0.01)
        self.assertIsNone(co.pig_temperature(celsius, [100, 100, 150, 118]))  # no warm pixels: no result


class CaptureWithFakeHardware(unittest.TestCase):
    """Runs capture_once.run() through the real worker modules with the camera, Lepton and model faked."""

    def test_end_to_end(self):
        import cv2
        import os
        os.environ['PIG_MODEL_PATH'] = '/nonexistent.onnx'
        os.environ.setdefault('PIG_PREVIEW_DIR', tempfile.mkdtemp())
        import thermal_worker as tw
        import yolo_worker as yw

        class FakeLepton:
            last_error = ''
            def capture(self):
                k = np.full((120, 160), int((24 + 273.15) * 100), dtype=np.uint16)
                k[30:60, 20:60] = int((37.5 + 273.15) * 100)          # pig 1 (lying)
                k[70:100, 100:140] = int((35.0 + 273.15) * 100)       # pig 2 (standing)
                return k
            def close(self):
                pass

        class FakeDetector:
            def __init__(self, path):
                pass
            def detect(self, frame):
                return [(np.array([80, 120, 240, 240.]), .97, 1, 'laying'), (np.array([400, 280, 560, 400.]), .96, 0, 'standing')], 5.0

        class FakeCam:
            def __init__(self, *a): pass
            def set(self, *a): return True
            def isOpened(self): return True
            def read(self): return True, np.full((480, 640, 3), 90, dtype=np.uint8)
            def release(self): pass

        saved = (tw.Lepton3, yw.YoloOnnxDetector, cv2.VideoCapture)
        tw.Lepton3, yw.YoloOnnxDetector, cv2.VideoCapture = FakeLepton, FakeDetector, FakeCam
        try:
            out = Path(tempfile.mkdtemp())
            cfg = {**co.CAPTURE_DEFAULTS, 'rgb_frame_spacing_s': 0, 'rgb_warmup_frames': 1}
            r = co.run(out, cfg, '/nonexistent.onnx', {'next_id': 1, 'pigs': []}, None, 0.5, 'test')
        finally:
            tw.Lepton3, yw.YoloOnnxDetector, cv2.VideoCapture = saved
        self.assertTrue(r['ok'], r)
        self.assertTrue(r['thermal_valid'])
        self.assertEqual([(p['id'], p['lying']) for p in r['pigs']], [(1, True), (2, False)])
        self.assertAlmostEqual(r['pigs'][0]['temp_c'], 38.0, delta=0.05)   # 37.5 + 0.5 offset
        self.assertAlmostEqual(r['pigs'][1]['temp_c'], 35.5, delta=0.05)
        self.assertEqual(r['next_id'], 3)
        for name in ('rgb.jpg', 'thermal.jpg'):
            self.assertTrue((out / name).stat().st_size < co.MAX_JPEG_BYTES)


# ---------------------------------------------------------------- controller
class FakeClient:
    def __init__(self):
        self.msgs = []
    def publish(self, topic, payload, qos=0, retain=False):
        self.msgs.append((topic.split('/', 2)[2], payload))
    def of(self, suffix):
        return [json.loads(p) for s, p in self.msgs if s == suffix]
    def loop_stop(self):
        pass


def scripted_capture(pigs_by_seq):
    calls = []
    def capture(out, previous, caption):
        seq = int(out.name)
        calls.append(seq)
        pigs = pigs_by_seq(seq)
        return {'ok': True, 'pigs': pigs, 'next_id': max([previous['next_id']] + [p['id'] + 1 for p in pigs]),
                'thermal_valid': True, 'images': {}}
    capture.calls = calls
    return capture


def make(data_dir, capture, sms=None, interval=0.15, duration=0.75, rule=3, **cfg_extra):
    cfg = inlayo8.load_config(Path('/nonexistent.json'))
    cfg['device_id'] = 'proto-test'
    cfg['test'].update(interval_s=interval, duration_s=duration)
    cfg['screening'].update(abnormal_lying_consecutive_captures=rule)
    cfg['gsm'].update(retry_delay_s=0)
    for k, v in cfg_extra.items():
        cfg[k].update(v) if isinstance(v, dict) else cfg.__setitem__(k, v)
    sent = []
    def send(message, gsm, stage):
        sent.append(message)
        if sms:
            sms(stage)
    p = inlayo8.Prototype(cfg, Path('/m.onnx'), Path('/c.json'), data_dir=data_dir, capture_fn=capture, sms_send=send)
    p.client = FakeClient()
    p.sent = sent
    return p


def drive(p, until, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        p.tick()
        if until(p):
            return True
        time.sleep(0.02)
    raise AssertionError(f'timed out; state={p.sess and p.sess["state"]} status={p.status}')


PIG1_LYING_HOT = {'id': 1, 'box': [0, 0, 10, 10], 'lying': True, 'temp_c': 37.2}
PIG2_STANDING_HOT = {'id': 2, 'box': [50, 50, 60, 60], 'lying': False, 'temp_c': 38.5}


class Controller(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_full_test_session_alerts_and_sends_one_sms(self):
        cap = scripted_capture(lambda seq: [PIG1_LYING_HOT, PIG2_STANDING_HOT])
        p = make(self.dir, cap)
        self.assertTrue(p.start_session(uid='sess-abc123', pen='PEN-07', test=True,
                                        server_time=inlayo8.iso(time.time() + 100)))
        drive(p, lambda p: p.sess['state'] == 'finished')
        samples = p.client.of('sample')
        self.assertEqual([s['seq'] for s in samples], [0, 1, 2, 3, 4, 5])      # 0.75 s / 0.15 s -> 6 captures
        self.assertGreater(inlayo8.parse_time(samples[0]['ts']), time.time() + 90)  # dashboard clock applied
        row = samples[3]['pigs'][0]
        self.assertEqual((row['pig_id'], row['posture'], row['behavioral_status'], row['thermal_status']), (1, 'Lying', 'Abnormal', 'Elevated'))
        self.assertEqual(row['lying_s'], 4 * 0.15)
        res = p.sess['result']
        self.assertEqual(res['flagged'], [1])
        self.assertEqual(len(p.sent), 1)
        self.assertIn('[TEST] PIG ALERT PEN-07', p.sent[0])
        final = p.client.of('session')[-1]
        self.assertEqual((final['state'], final['sms']['state']), ('finished', 'submitted'))
        self.assertNotIn('message', final['sms'])
        stored = p.store.samples('sess-abc123')
        self.assertEqual(len(stored), 6)                                        # local log has everything

    def test_resume_after_restart_and_record_missed_slots(self):
        cap = scripted_capture(lambda seq: [PIG1_LYING_HOT])
        p = make(self.dir, cap, interval=0.2, duration=2.0)
        p.start_session(test=True)
        drive(p, lambda p: p.sess['next_seq'] >= 2)
        p.store.close()
        time.sleep(0.75)                                                        # "power cut" spanning several slots
        q = make(self.dir, cap, interval=0.2, duration=2.0)
        self.assertEqual(q.sess['state'], 'running')
        drive(q, lambda q: q.sess['state'] == 'finished')
        stored = q.store.samples(q.sess['uid'])
        self.assertEqual([s['seq'] for s in stored], list(range(11)))
        missed = [s for s in stored if not s['ok']]
        self.assertGreaterEqual(len(missed), 2)
        self.assertTrue(all(r['behavioral_status'] == 'No data' for s in missed for r in s['pigs']))

    def test_dashboard_stop_ends_early_without_sms(self):
        p = make(self.dir, scripted_capture(lambda seq: [PIG1_LYING_HOT]), interval=0.2, duration=5)
        p.start_session(uid='sess-stop01', test=True)
        drive(p, lambda p: p.sess['next_seq'] >= 4)
        p.cmds.put({'action': 'stop', 'uid': 'sess-stop01'})
        drive(p, lambda p: p.sess['state'] == 'ended_early')
        self.assertEqual(p.sess['sms']['state'], 'skipped')
        self.assertEqual(p.sent, [])
        self.assertEqual(p.sess['result']['flagged'], [1])                      # result is still recorded

    def test_replay_start_refusal_and_unsupported_commands(self):
        p = make(self.dir, scripted_capture(lambda seq: []), interval=0.1, duration=10)
        p.start_session(uid='sess-replay', test=True)
        drive(p, lambda p: p.sess['next_seq'] >= 3)
        before = len(p.client.of('sample'))
        p.cmds.put({'action': 'replay', 'uid': 'sess-replay', 'seqs': [0, 2]})
        p.cmds.put({'action': 'start', 'uid': 'sess-other1'})
        p.cmds.put({'action': 'reset'})
        p.tick()
        replayed = p.client.of('sample')[before:]
        self.assertEqual([s['seq'] for s in replayed[:2]], [0, 2])
        kinds = [e['kind'] for e in p.client.of('event')]
        self.assertIn('start_refused', kinds)
        self.assertIn('unsupported', kinds)
        self.assertEqual(p.sess['uid'], 'sess-replay')
        self.assertEqual(replayed[0]['pigs'], [])                               # a capture with no pigs is still a sample

    def test_restart_during_sms_is_reported_unknown_not_resent(self):
        p = make(self.dir, scripted_capture(lambda seq: []))
        p.start_session(uid='sess-sms001', test=True)
        p.store.update('sess-sms001', state='evaluating', sms={'state': 'sending', 'message': 'x'})
        p.store.close()
        q = make(self.dir, scripted_capture(lambda seq: []))
        self.assertEqual((q.sess['state'], q.sess['sms']['state']), ('finished', 'unknown'))
        self.assertEqual(q.sent, [])


class SmsRetries(unittest.TestCase):
    def test_retry_only_before_the_text_reaches_the_modem(self):
        calls = []
        def flaky(message, gsm, stage):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError('Not registered')
        self.assertEqual(inlayo8.send_with_retries('m', {'attempts': 3}, flaky, sleep=lambda s: None)[0], 'submitted')
        self.assertEqual(len(calls), 3)
        calls.clear()
        def lost(message, gsm, stage):
            calls.append(1); stage['body_sent'] = True; raise TimeoutError('no reply')
        state, detail = inlayo8.send_with_retries('m', {'attempts': 3}, lost, sleep=lambda s: None)
        self.assertEqual((state, len(calls)), ('unconfirmed', 1))
        state, _ = inlayo8.send_with_retries('m', {'attempts': 2}, lambda *a: (_ for _ in ()).throw(RuntimeError('x')), sleep=lambda s: None)
        self.assertEqual(state, 'failed')


if __name__ == '__main__':
    unittest.main()
