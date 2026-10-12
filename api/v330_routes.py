"""Authenticated dashboard routes; the host enforces session login and CSRF."""
from pathlib import Path
import os
import yaml
from flask import jsonify, request, session
from core.runtime_store import EventHistory
from core.live_capacity import max_real_tickers
from core.ticker_execution import normalize_share_count
from core.execution_diagnostics import ExecutionDiagnostics


def register(app, host):
    @app.route('/api/live-settings', methods=['GET', 'POST'])
    def live_settings_v330():
        with host['_ENGINE_CONTROL_LOCK']:
            cfg = host['_read_config_runtime']()
            live = cfg.get('live_trading', {}) or {}
            if request.method == 'GET':
                return jsonify(status='ok', max_real_tickers=max_real_tickers(live),
                    max_shares_per_order=normalize_share_count(live.get('max_shares_per_order', 1)),
                    capacity=live.get('capacity', {}), execution_engine=cfg.get('execution_engine', {}))
            if host['BOT_STATE'].get('running'):
                return jsonify(status='error', message='Stop the engine before changing REAL limits.'), 409
            data = request.get_json(silent=True) or {}
            try:
                fields = {'max_real_tickers', 'max_shares_per_order'}
                if not isinstance(data, dict) or not data or not set(data) <= fields:
                    raise ValueError('Provide max_real_tickers and/or max_shares_per_order')
                limit = max_real_tickers(live)
                shares_limit = normalize_share_count(live.get('max_shares_per_order', 1))
                old_limits = {'max_real_tickers': limit, 'max_shares_per_order': shares_limit}
                rows = host['_ticker_execution_rows'](cfg, persist_missing=False)
                if 'max_real_tickers' in data:
                    limit = max_real_tickers(data)
                    # A ceiling can be configured before adding more symbols.
                    # Saving it never selects tickers or changes their quantities.
                    selected = [row['ticker'] for row in rows if row['mode'] == 'REAL']
                    if limit < len(selected):
                        raise ValueError(
                            f'Hay {len(selected)} tickers guardados en REAL. '
                            f'Cambie {len(selected) - limit} a PAPER y guarde sus filas '
                            f'antes de reducir el límite a {limit}.')
                if 'max_shares_per_order' in data:
                    shares_limit = normalize_share_count(data['max_shares_per_order'])
                    oversized = [row['ticker'] for row in rows
                                 if row['mode'] == 'REAL' and row['shares'] > shares_limit]
                    if oversized:
                        raise ValueError('Reduce configured REAL shares first: ' + ', '.join(oversized))
                    if not live.get('per_ticker_execution', False):
                        risk = cfg.get('risk', {}) or {}
                        if any(int(risk.get(key, 0) or 0) > shares_limit
                               for key in ('fixed_shares', 'max_acciones_por_orden')):
                            raise ValueError('Reduce the global risk share settings before lowering the REAL share limit')
                new_limits = {'max_real_tickers': limit, 'max_shares_per_order': shares_limit}
                EventHistory(live.get('authorization_audit_path', 'data/real_mode_audit.jsonl')).append({
                    'event': 'REAL_CAPACITY_CHANGE_REQUESTED', 'actor': session.get('username'),
                    'old_limit': old_limits['max_real_tickers'], 'new_limit': limit,
                    'previous': old_limits, 'requested': new_limits})
                cfg.setdefault('live_trading', {}).update(**new_limits, max_tickers=limit)
                path = Path(host['CONFIG_PATH'])
                tmp = path.with_suffix('.v330.tmp')
                with tmp.open('w', encoding='utf-8') as handle:
                    yaml.safe_dump(cfg, handle, sort_keys=False, allow_unicode=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            except (KeyError, ValueError, TypeError, OverflowError, OSError) as exc:
                return jsonify(status='error', message=str(exc)), 400
            return jsonify(status='ok', **new_limits)

    @app.route('/api/live-authorization/<ticker>', methods=['GET', 'POST', 'DELETE'])
    def authorization_v330(ticker):
        ticker = ticker.strip().upper()
        if ticker not in host['BOT_STATE'].get('tickers', []):
            return jsonify(status='error', message='Unknown ticker'), 404
        with host['_ENGINE_CONTROL_LOCK']:
            trainer = host['_ai_trainer_for_status']()
            model = trainer.production_snapshot(ticker)
            auth = trainer.manual_real_authorization
            if request.method == 'GET':
                return jsonify(status='ok', model=model,
                    authorization=auth.active(ticker, model.get('model_version')),
                    history=auth.history.recent(ticker, 50))
            if host['BOT_STATE'].get('running'):
                return jsonify(status='error', message='Stop the engine before changing REAL authorization.'), 409
            try:
                if request.method == 'DELETE':
                    auth.revoke(ticker, session.get('username'))
                    return jsonify(status='ok', authorization=None)
                data = request.get_json(silent=True) or {}
                if not model.get('loaded') or not trainer.modelo_disponible(ticker):
                    raise ValueError('A compatible, loaded PRODUCTION model is required')
                if data.get('confirmation') != f'AUTHORIZE REAL {ticker}':
                    raise ValueError(f'Type AUTHORIZE REAL {ticker} to confirm')
                if data.get('model_version') != model.get('model_version'):
                    raise ValueError('Model changed; reload the model details before authorizing')
                if not isinstance(data.get('bypass_probability', False), bool):
                    raise ValueError('bypass_probability must be a boolean')
                row = auth.authorize(ticker, model['model_version'], str(data.get('reason', '')).strip(),
                    session.get('username'), data.get('bypass_probability', False))
            except (ValueError, OSError) as exc:
                return jsonify(status='error', message=str(exc)), 400
            return jsonify(status='ok', authorization=row)

    @app.route('/api/ai/threshold/<ticker>', methods=['GET', 'POST', 'DELETE'])
    def ai_threshold_v3319(ticker):
        """Operator AI threshold for one ticker (v3.3.19).

        v3.3.25: changes are allowed while the engine runs. The AI reads the
        store on every decision, so a change applies to the next signal and
        never touches an open position. Stopping the engine to change it ran
        the kill switch, so in practice thresholds were never adjusted.
        """
        from core.ai_threshold_overrides import MAX_THRESHOLD, MIN_THRESHOLD, validate_threshold
        ticker = ticker.strip().upper()
        if ticker not in host['BOT_STATE'].get('tickers', []):
            return jsonify(status='error', message='Unknown ticker'), 404
        with host['_ENGINE_CONTROL_LOCK']:
            trainer = host['_ai_trainer_for_status']()
            store = trainer.umbrales_operador
            with trainer._lock:
                model_threshold = trainer._umbrales.get(ticker)
            def payload(**extra):
                return jsonify(status='ok', ticker=ticker, model_threshold=model_threshold,
                    override=store.row(ticker), applied_threshold=trainer.umbral_decision(ticker),
                    model_loaded=trainer.modelo_disponible(ticker), min_threshold=MIN_THRESHOLD,
                    max_threshold=MAX_THRESHOLD, load_error=store.load_error or None, **extra)
            if request.method == 'GET':
                return payload()
            cfg = host['_read_config_runtime']()
            log = host.get('_log') or (lambda *args, **kwargs: None)
            audit = EventHistory(cfg.get('live_trading', {}).get(
                'authorization_audit_path', 'data/real_mode_audit.jsonl'))
            # Audit first (as live-settings does): no change without a record.
            try:
                if request.method == 'DELETE':
                    previous = store.row(ticker)
                    if previous:
                        audit.append({'event': 'AI_THRESHOLD_OVERRIDE_CLEARED', 'ticker': ticker,
                                      'actor': session.get('username'), 'previous': previous})
                        store.clear(ticker)
                        log("INFO", f"[IA] {ticker}: umbral manual eliminado; vuelve al umbral "
                                    "del modelo para las próximas señales")
                    return payload()
                data = request.get_json(silent=True) or {}
                threshold = validate_threshold(data.get('threshold'))
                reason = str(data.get('reason', '') or '').strip() or 'Ajuste desde el dashboard'
                audit.append({'event': 'AI_THRESHOLD_OVERRIDE_SET', 'ticker': ticker,
                              'actor': session.get('username'), 'threshold': threshold,
                              'model_threshold': model_threshold, 'previous': store.row(ticker),
                              'reason': reason[:300]})
                store.set(ticker, threshold, actor=session.get('username'), reason=reason,
                          model_threshold=model_threshold)
                log("WARN", f"[IA] {ticker}: umbral manual {threshold:.0%} aplicado a las "
                            "próximas señales (posiciones abiertas sin cambios)")
            except (ValueError, OSError) as exc:
                return jsonify(status='error', message=str(exc)), 400
            return payload()

    @app.route('/api/ai/history/<ticker>')
    def ai_history_v330(ticker):
        ticker = ticker.strip().upper()
        if ticker not in host['BOT_STATE'].get('tickers', []):
            return jsonify(status='error', message='Unknown ticker'), 404
        trainer = host['_ai_trainer_for_status']()
        cfg = host['_read_config_runtime']()
        path = cfg.get('live_trading', {}).get('model_promotion', {}).get('audit_path', 'data/model_promotion_audit.jsonl')
        return jsonify(
            status='ok',
            evaluations=trainer.retraining.history.recent(ticker, 200),
            promotions=EventHistory(path).recent(ticker, 200),
            model=trainer.production_snapshot(ticker),
            shadow_model=(trainer.shadow_snapshot(ticker) if hasattr(trainer, 'shadow_snapshot') else {}),
        )

    @app.route('/api/execution-diagnostics')
    def execution_diagnostics_v330():
        cfg = host['_read_config_runtime']()
        ledger = ExecutionDiagnostics(cfg.get('logging', {}).get('execution_diagnostics', 'data/execution_diagnostics.jsonl'))
        engine = host.get('_ENGINE')
        return jsonify(status='ok', **ledger.report(),
            capacity=getattr(getattr(engine, 'real_capacity', None), 'last', {}),
            quarantine=getattr(engine, '_ticker_quarantine', {}))
