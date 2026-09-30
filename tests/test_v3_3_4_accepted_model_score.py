import json
import threading
from pathlib import Path
from types import SimpleNamespace


def test_shadow_snapshot_keeps_original_accepted_score():
    from agents.training import EntrenadorAgentes

    trainer = object.__new__(EntrenadorAgentes)
    trainer._lock = threading.RLock()
    trainer._shadow_snapshots = {
        "BA": {
            "loaded": True,
            "model_tier": "SHADOW",
            "balanced_accuracy": 57.0,
            "validation_date": "2026-08-18T19:45:00Z",
            "model_version": "accepted-ba-v1",
        }
    }

    first = trainer.shadow_snapshot("BA")
    assert first["balanced_accuracy"] == 57.0
    first["balanced_accuracy"] = 1.0
    # Caller cannot mutate the authoritative installed-model snapshot.
    assert trainer.shadow_snapshot("BA")["balanced_accuracy"] == 57.0


def test_api_status_wires_installed_shadow_snapshot_separately():
    source = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    assert 'row["shadow_model"] = shadow_model' in source
    assert 'row["accepted_shadow_balanced_accuracy"]' in source
    assert 'shadow_model.get("balanced_accuracy") if shadow_model.get("loaded") else None' in source
    assert 'row["accepted_shadow_validation_date"]' in source
    assert 'row["accepted_shadow_model_version"]' in source

def test_dashboard_labels_historical_accepted_score_separately():
    html = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "BA aceptada SHADOW" in html
    assert "Último candidato" in html
    assert "accepted_shadow_balanced_accuracy" in html
    assert "un reentrenamiento posterior REJECTED no la reemplaza" in html
    assert 'colspan="16"' in html


def test_dashboard_renders_accepted_57_green_while_new_candidate_is_rejected():
    import shutil
    import subprocess
    import pytest

    if shutil.which("node") is None:
        pytest.skip("Node is required for dashboard rendering check")
    html_path = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8');
const take=(start,end)=>html.slice(html.indexOf(start),html.indexOf(end));
const nodes={aiLifecycleSummary:{textContent:'',className:''},aiLifecycleRows:{innerHTML:''}};
const context=vm.createContext({
  S:{aiStatus:[],tickerExecution:[]},
  el:id=>nodes[id]||null,
  api:async()=>({json:async()=>({status:'ok',summary:{ready_models:1,eligible_observations:100,veto_total:0,pending:0,training:0},tickers:[{ticker:'BA',dashboard_model_status:'READY',new_observations:38,retrain_samples:50,eligible_observations:339,quarantined_observations:17290,status:'READY_REVIEW',last_shadow_candidate_accepted:false,last_result:'REJECTED',accepted_shadow_balanced_accuracy:57.0,accepted_shadow_validation_date:'2026-08-18T19:45:00Z',last_balanced_accuracy:46.09,last_shadow_incumbent_balanced_accuracy:53.59,last_shadow_delta_pp:-7.50,last_production_gate_passed:false,last_decision_threshold:0.68,last_evaluation_utc:'2026-09-22T19:47:00Z',last_reason:'balanced_accuracy',shadow_model_loaded:true,production_model_loaded:false}]})}),
  renderTickerExecution:()=>{},
  escHtml:s=>String(s||'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))
});
vm.runInContext(take('async function fetchAIStatus()', '// ─────────────────────────────────────────────────────────\n// AI TRAINING'), context);
(async()=>{await context.fetchAIStatus();process.stdout.write(nodes.aiLifecycleRows.innerHTML);})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(["node", "-e", script, str(html_path)], capture_output=True, text=True, check=True)
    rendered = result.stdout
    assert "57.00%" in rendered
    assert "46.09%" in rendered
    assert "53.59%" in rendered
    assert "REJECTED" in rendered
    assert 'class="ai-accepted-score"' in rendered
