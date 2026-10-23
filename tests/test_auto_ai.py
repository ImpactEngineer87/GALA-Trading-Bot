"""Offline automatic SHADOW scheduling, private settings and admission tests."""
import copy
import json
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

import server
from agents.training import EntrenadorAgentes
from core.auto_ai import AutoAIService, policy, update_enabled, window


def config(enabled=True):
    return {"scheduler": {"modo": "manual", "hora_fin": "15:45"},
            "market": {"tickers": ["RCL", "MSFT", "ADBE"]},
            "agents": {"entrenamiento": {"automatic_retraining": {
                "enabled": enabled, "trigger_samples": 50,
                "state_path": "data/preserved.json"}}}}


class AutoAIWorkerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.now = datetime(2026, 10, 23, 20, 16, tzinfo=timezone.utc)
        self.busy = False
        self.trainer = SimpleNamespace(
            _eligible_batch_running=False, _entrenando=set(),
            retraining=SimpleNamespace(eligible_tickers=Mock(return_value=["RCL"])),
            solicitar_reentreno_todos=Mock(return_value=True))
        self.get_trainer = Mock(return_value=self.trainer)
        self.service = AutoAIService(lambda: self.cfg, self.get_trainer,
            lambda: self.busy, lambda cfg: cfg["market"]["tickers"], now=lambda: self.now)

    def test_off_never_initializes_trainer_or_consumes_pending_counts(self):
        self.cfg = config(False)
        self.assertEqual(self.service.tick()["state"], "OFF")
        self.get_trainer.assert_not_called()

    def test_manual_trading_scheduler_still_queues_post_session_shadow(self):
        self.assertEqual(self.service.tick()["state"], "QUEUED")
        self.trainer.solicitar_reentreno_todos.assert_called_once_with(["RCL"], solo_elegibles=True)
        self.assertFalse(self.trainer.auto_activate_production)
        self.assertTrue(self.trainer.auto_retrain_only_after_session)
        self.assertTrue(self.trainer.automatic_training_allowed())

    def test_active_engine_or_local_exposure_defers_without_touching_trainer(self):
        self.busy = True
        self.assertEqual(self.service.tick()["state"], "WAIT_ENGINE")
        self.assertFalse(self.service.allowed_now())
        self.get_trainer.assert_not_called()

    def test_intraday_waits_until_1615_new_york(self):
        self.now = datetime(2026, 10, 23, 13, 27, tzinfo=timezone.utc)
        state = self.service.tick()
        self.assertEqual(state["state"], "WAIT_SESSION")
        self.assertEqual(state["next_window_ny"], "2026-10-23T16:15:00-04:00")
        self.get_trainer.assert_not_called()

    def test_weekends_and_dst_are_evaluated_in_new_york(self):
        self.now = datetime(2026, 10, 24, 22, tzinfo=timezone.utc)
        self.assertEqual(self.service.tick()["next_window_ny"], "2026-10-26T16:15:00-04:00")
        self.assertEqual(window(self.cfg, datetime(2026, 11, 2, 20, 30, tzinfo=timezone.utc)),
                         (False, "2026-11-02T16:15:00-05:00"))
        self.assertTrue(window(self.cfg, datetime(2026, 11, 2, 21, 15, tzinfo=timezone.utc))[0])

    def test_configured_later_end_and_invalid_schedule(self):
        self.cfg["scheduler"]["hora_fin"] = "17:00"
        self.assertEqual(policy(self.cfg)["not_before_ny"], "17:00")
        self.assertEqual(self.service.tick()["state"], "WAIT_SESSION")
        self.cfg["scheduler"]["hora_fin"] = "25:00"
        self.assertEqual(self.service.tick()["state"], "ERROR")
        self.assertFalse(self.service.allowed_now())
        self.get_trainer.assert_not_called()

    def test_no_fifty_new_closes_does_not_force_training(self):
        self.trainer.retraining.eligible_tickers.return_value = []
        self.assertEqual(self.service.tick()["state"], "WAIT_CLOSES")
        self.trainer.solicitar_reentreno_todos.assert_not_called()

    def test_busy_worker_is_not_queued_twice(self):
        self.trainer._eligible_batch_running = True
        self.assertEqual(self.service.tick()["state"], "TRAINING")
        self.trainer.solicitar_reentreno_todos.assert_not_called()

    def test_same_deferred_data_is_not_retried_each_minute(self):
        self.assertEqual(self.service.tick()["state"], "QUEUED")
        self.assertEqual(self.service.tick()["state"], "WAIT_NEW_DATA")
        self.trainer.solicitar_reentreno_todos.assert_called_once()
        self.trainer.retraining.eligible_tickers.return_value = ["RCL", "MSFT"]
        self.assertEqual(self.service.tick()["state"], "QUEUED")
        self.assertEqual(self.trainer.solicitar_reentreno_todos.call_count, 2)

    def test_new_source_data_allows_deferred_attempt_without_resetting_counts(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.trainer.feedback_path = root / "feedback.csv"
        self.trainer.feedback_path.write_text("before")
        self.assertEqual(self.service.tick()["state"], "QUEUED")
        self.assertEqual(self.service.tick()["state"], "WAIT_NEW_DATA")
        self.trainer.feedback_path.write_text("new verified observation")
        self.assertEqual(self.service.tick()["state"], "QUEUED")

    def test_disabled_or_unreadable_config_rechecked_between_tickers(self):
        self.assertTrue(self.service.allowed_now())
        self.cfg = config(False)
        self.assertFalse(self.service.allowed_now())
        self.service.get_config = Mock(side_effect=OSError("unreadable"))
        self.assertFalse(self.service.allowed_now())
        self.assertEqual(self.service.tick()["state"], "ERROR")


class AutoAISettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "config.yaml"
        self.cfg = config(False)
        self.cfg.update(risk={"max_daily_loss": 200}, broker={"secret": "preserve-me"})
        self.cfg["agents"]["entrenamiento"]["automatic_retraining"].update(
            auto_activate_production=True, only_after_session=False)
        self.path.write_text(yaml.safe_dump(self.cfg))
        self.path.chmod(0o600)
        self.files = [self.root / name for name in (".env", "model.pkl", "trades.csv", "state.json", "history.jsonl")]
        for path in self.files:
            path.write_bytes(b"must remain byte-identical")
        self.original = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.files}
        self.service = Mock()
        self.service.snapshot.return_value = {"state": "NOT_CHECKED", "message": "Comprobando"}
        self.enterContext(patch.object(server, "CONFIG_PATH", str(self.path)))
        self.enterContext(patch.object(server, "_AUTO_AI_SERVICE", self.service))
        self.enterContext(patch.object(server, "_ENGINE", None))
        self.enterContext(patch.object(server, "_engine_thread", None))
        self.enterContext(patch.object(server, "_AI_STATUS_TRAINER", None))
        self.enterContext(patch.dict(server.BOT_STATE, {"running": False}))
        self.network = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("Auto AI settings must never contact broker")))
        self.client = server.app.test_client()

    def tearDown(self):
        self.network.assert_not_called()
        for path, (content, mtime) in self.original.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, mtime)

    def login(self):
        with self.client.session_transaction() as session:
            session.update(logged_in=True, csrf_token="offline-test-token")

    def post(self, value):
        return self.client.post("/api/ai/automatic", json=value,
            headers={"X-CSRF-Token": "offline-test-token"})

    def test_auth_and_csrf_are_required(self):
        original = self.path.read_bytes()
        self.assertEqual(self.post({"enabled": True}).status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/api/ai/automatic", json={"enabled": True}).status_code, 403)
        self.assertEqual(self.path.read_bytes(), original)

    def test_get_is_read_only_and_reports_shadow_policy(self):
        self.login()
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        response = self.client.get("/api/ai/automatic")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["policy"]["enabled"])
        self.assertFalse(response.json["policy"]["auto_activate_production"])
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        self.service.start.assert_not_called()

    def test_status_uses_saved_flag_instead_of_stale_running_trainer(self):
        self.login()
        stale = SimpleNamespace(auto_retrain_enabled=True, retraining_status=lambda tickers: [],
                                retrain_warning_samples=30, retrain_trigger_samples=50,
                                signal_learning_weight=.35, signal_learning_max_ratio=1,
                                round_trip_cost_bps=2, economic_min_trades=10)
        with patch.object(server, "_ai_trainer_for_status", return_value=stale), patch.object(
                server, "_veto_summary_rows", return_value=[]), patch.object(
                server, "_learning_summary_rows", return_value=[]):
            response = self.client.get("/api/ai/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["automatic_retraining"])
        self.assertFalse(response.json["auto_ai_policy"]["enabled"])

    def test_persisted_switch_preserves_secrets_risk_threshold_paths_and_mode(self):
        self.login()
        for enabled in (True, False):
            response = self.post({"enabled": enabled})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["preserves_x50"])
            self.assertIs(response.json["policy"]["enabled"], enabled)
            fresh = yaml.safe_load(self.path.read_text())
            expected = copy.deepcopy(self.cfg)
            expected["agents"]["entrenamiento"]["automatic_retraining"].update(
                enabled=enabled, only_after_session=True, auto_activate_production=False)
            self.assertEqual(fresh, expected)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.service.wake.call_count, 2)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard checks")
    def test_actual_dashboard_fetch_requests_are_accepted_by_authenticated_route(self):
        """Use the real API helper and Fetch Request serialization, then Flask."""
        dashboard = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
        script = r"""
const fs=require('fs'),vm=require('vm');
const html=fs.readFileSync(process.argv[1],'utf8'),requests=[];
const elements={};
const context=vm.createContext({Headers,csrfToken:'offline-test-token',
  S:{autoAIEnabled:false},el:id=>elements[id]??={},toast:()=>{},
  renderAutoAIControl:()=>{},fetchAIStatus:async()=>{},
  fetch:async(url,opts)=>{
    const request=new Request('http://127.0.0.1:5000'+url,opts);
    const body=await request.text();
    requests.push({url,method:request.method,headers:Object.fromEntries(request.headers),body});
    return {ok:true,status:200,json:async()=>({status:'ok',
      policy:{enabled:JSON.parse(body).enabled},service:{state:'NOT_CHECKED'}})};
  }});
let a=html.indexOf('async function api('),b=html.indexOf('// LOGIN / LOGOUT',a);
vm.runInContext(html.slice(a,b),context);
a=html.indexOf('async function toggleAutoAI(');b=html.indexOf('async function triggerTraining',a);
vm.runInContext(html.slice(a,b),context);
(async()=>{
  for(const enabled of [false,true]){
    context.S.autoAIEnabled=enabled;
    await context.toggleAutoAI();
  }
  process.stdout.write(JSON.stringify(requests));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(dashboard)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        captured = json.loads(result.stdout)
        self.assertEqual(len(captured), 2)
        self.login()
        for item, enabled in zip(captured, (True, False)):
            with self.subTest(enabled=enabled):
                self.assertEqual(item["url"], "/api/ai/automatic")
                self.assertEqual(item["method"], "POST")
                response = self.client.post(item["url"], data=item["body"], headers=item["headers"])
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(item["headers"].get("content-type"), "application/json")
                self.assertIs(response.json["policy"]["enabled"], enabled)
                self.assertFalse(response.json["policy"]["auto_activate_production"])
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_invalid_inputs_never_write(self):
        self.login()
        before = self.path.read_bytes()
        for value in (None, [], {}, {"enabled": "false"}, {"enabled": 1},
                      {"enabled": True, "auto_activate_production": True}):
            with self.subTest(value=value):
                self.assertEqual(self.post(value).status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            update_enabled(self.path, 1)

    def test_engine_start_and_manual_queue_wait_for_existing_training(self):
        server._AI_STATUS_TRAINER = SimpleNamespace(_eligible_batch_running=True, _entrenando=set())
        with patch.object(server, "_start_engine") as start:
            ok, message = server._start_engine_controlled("manual")
            self.assertFalse(ok)
            self.assertIn("evaluación IA", message)
            start.assert_not_called()
        self.login()
        response = self.client.post("/api/ai/train", headers={"X-CSRF-Token": "offline-test-token"})
        self.assertEqual(response.status_code, 409)

    def test_local_positions_block_training_even_with_running_false(self):
        server._ENGINE = SimpleNamespace(_activo=False, _posiciones={"TSLA": object()})
        self.assertTrue(server._ai_engine_busy())
        self.login()
        response = self.client.post("/api/ai/train", headers={"X-CSRF-Token": "offline-test-token"})
        self.assertEqual(response.status_code, 409)

    def test_polling_does_not_replace_a_worker_when_it_writes_artifacts(self):
        worker = SimpleNamespace(_eligible_batch_running=True, _entrenando=set())
        server._AI_STATUS_TRAINER = worker
        with patch("agents.training.EntrenadorAgentes") as factory:
            self.assertIs(server._ai_trainer_for_status(), worker)
        factory.assert_not_called()


class AutomaticTrainingAdmissionTests(unittest.TestCase):
    def trainer(self):
        trainer = EntrenadorAgentes.__new__(EntrenadorAgentes)
        trainer._lock = threading.RLock()
        trainer._entrenando = set()
        trainer._eligible_batch_running = False
        trainer.auto_retrain_enabled = True
        trainer.auto_activate_production = True  # unsafe legacy option must be ignored
        trainer.retraining = Mock()
        trainer.retraining.eligible_tickers.return_value = ["RCL", "MSFT"]
        trainer.retraining.register_closed.return_value = {"total_observations": 511}
        trainer.entrenar_ticker = Mock(return_value={"shadow_candidate_accepted": True,
            "shadow_candidate_reason": "candidato Shadow aceptado"})
        return trainer

    def test_batch_rechecks_off_between_tickers_and_never_promotes(self):
        trainer = self.trainer()
        def train(*args, **kwargs):
            trainer.auto_retrain_enabled = False
            return {"shadow_candidate_accepted": True}
        trainer.entrenar_ticker.side_effect = train
        result = trainer._retrain_eligible_batch(["RCL", "MSFT"])
        self.assertEqual(list(result), ["RCL"])
        trainer.entrenar_ticker.assert_called_once_with("RCL", allow_production_activation=False, automatic=True)
        trainer.retraining.register_closed.assert_called_once_with("RCL")
        self.assertFalse(trainer._eligible_batch_running)

    @patch("agents.training.threading.Thread")
    def test_batch_is_reserved_before_start_and_failed_start_releases_it(self, thread):
        trainer = self.trainer()
        self.assertTrue(trainer.solicitar_reentreno_todos(["RCL", "MSFT"], solo_elegibles=True))
        self.assertTrue(trainer._eligible_batch_running)
        self.assertFalse(trainer.solicitar_reentreno_todos(["RCL"], solo_elegibles=True))
        thread.assert_called_once()
        trainer._eligible_batch_running = False
        thread.return_value.start.side_effect = RuntimeError("cannot launch")
        self.assertFalse(trainer.solicitar_reentreno_todos(["RCL"], solo_elegibles=True))
        self.assertFalse(trainer._eligible_batch_running)

    def test_config_window_blocks_legacy_session_end_before_1615(self):
        trainer = self.trainer()
        trainer._automatic_training_config = config(True)
        with patch("core.auto_ai.window", return_value=(False, "later")), patch("agents.training.threading.Thread") as thread:
            self.assertFalse(trainer.solicitar_reentreno_todos(["RCL"], solo_elegibles=True))
        thread.assert_not_called()
        trainer.retraining.mark_training.assert_not_called()


@unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard checks")
class AutoAIDashboardTests(unittest.TestCase):
    def test_off_switch_can_be_enabled_and_failures_do_not_claim_success(self):
        dashboard = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const elements={},requests=[],toasts=[];
let failure=false;
const context=vm.createContext({S:{},el:id=>elements[id]??={},
  toast:(...args)=>toasts.push(args),
  api:async(url,opts)=>{requests.push([url,JSON.parse(opts.body)]);
    return {ok:!failure,json:async()=>failure?{status:'error',message:'No guardado'}:
      {status:'ok',policy:{enabled:true,not_before_ny:'16:15'},service:{state:'WAIT_SESSION',message:'Esperando ventana'}}};}});
let a=html.indexOf('function renderAutoAIControl('),b=html.indexOf('async function fetchAIStatus',a);
vm.runInContext(html.slice(a,b),context);
a=html.indexOf('async function toggleAutoAI(');b=html.indexOf('async function triggerTraining',a);
vm.runInContext(html.slice(a,b),context);
context.fetchAIStatus=async()=>{};
(async()=>{
  context.renderAutoAIControl({automatic_retraining:false});
  assert.equal(elements.autoAIButton.disabled,false);
  assert.equal(elements.autoAIButton.textContent,'Activar Auto IA');
  await context.toggleAutoAI();
  assert.equal(requests.length,1);
  assert.equal(requests[0][0],'/api/ai/automatic');
  assert.deepEqual(requests[0][1],{enabled:true});
  assert.equal(elements.autoAIButton.textContent,'Desactivar Auto IA');
  assert.match(elements.autoAIState.textContent,/Esperando ventana/);
  assert.match(elements.autoAIState.textContent,/Sólo SHADOW/);
  failure=true;
  await context.toggleAutoAI();
  assert.equal(context.S.autoAIEnabled,null);
  assert.equal(elements.autoAIButton.disabled,true);
  assert.equal(toasts.at(-1)[0],'err');
  assert.equal(requests.length,2);
  console.log('Auto AI dashboard controls passed');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(dashboard)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
