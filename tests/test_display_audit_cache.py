"""Cached display results must match full-ledger attribution after new evidence."""
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core.dashboard_evidence import annotate_history
from core.display_audit_cache import DisplayAuditCache
from core.order_audit import AUDIT_FIELDS
from core.signal_execution_evidence import annotate_signals, _key


class DisplayAuditCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder=self.enterContext(tempfile.TemporaryDirectory())
        self.path=Path(self.folder)/'audit.csv';self.cache=DisplayAuditCache()
        self.signal=dict(execution_mode='REAL',ticker='META',signal_id='s',date='2026-11-10')
        self.base=dict(modo='REAL',ticker='META',signal_id='s',position_id='p',
                       execution_id='e',timestamp_utc='2026-11-10T15:00:00Z')

    def append(self,**event):
        new=not self.path.exists()
        with self.path.open('a',encoding='utf-8',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=AUDIT_FIELDS)
            if new:writer.writeheader()
            writer.writerow({**self.base,**event})

    def assert_signal_parity(self):
        before=self.path.read_bytes()
        with self.path.open(encoding='utf-8-sig',newline='') as handle:
            full=list(csv.DictReader(handle))
        rows,available=self.cache.signals(self.path,{_key(self.signal,signal=True)})
        self.assertTrue(available)
        actual=annotate_signals([self.signal],rows)
        self.assertEqual(actual,annotate_signals([self.signal],full))
        self.assertEqual(self.path.read_bytes(),before)
        return actual[0]['execution_evidence']['state']

    def test_full_scan_parity_and_latest_contradictory_fill_wins(self):
        self.append(evento='ENTRY_SUBMITTED',order_id='o')
        self.assertEqual(self.assert_signal_parity(),'SUBMITTED')
        self.append(evento='ENTRY_FILLED',order_id='o',estado='FILLED')
        self.assertEqual(self.assert_signal_parity(),'FILL_RECORDED')
        self.append(evento='ENTRY_FILLED',order_id='o',estado='UNVERIFIED')
        self.assertEqual(self.assert_signal_parity(),'UNCERTAIN')
        self.append(evento='REVERSAL_ABORTED',motivo='REVERSAL_CLOSE_FAILED')
        self.assertEqual(self.assert_signal_parity(),'CLOSE_UNRESOLVED')

    def test_unrelated_receipts_do_not_invalidate_pnl_but_new_fill_does(self):
        self.append(evento='ENTRY_FILLED',order_id='o',estado='FILLED')
        revision,rows,ok=self.cache.history(self.path);self.assertTrue(ok)
        self.append(evento='STRATEGY_CANDLE_EVALUATED',detalle=json.dumps({'large':[1]*200}))
        same,again,_=self.cache.history(self.path)
        self.assertIs(same,revision);self.assertEqual(again,rows)
        self.append(evento='POSITION_CLOSED',order_id='close',estado='FILLED')
        changed,_,_=self.cache.history(self.path);self.assertIsNot(changed,revision)

    def test_multiline_partial_utf8_row_is_not_committed_until_complete(self):
        self.append(evento='ENTRY_SUBMITTED',order_id='o')
        self.assert_signal_parity()
        output=io.StringIO(newline='')
        csv.DictWriter(output,fieldnames=AUDIT_FIELDS).writerow({**self.base,
            'evento':'ENTRY_FILLED','estado':'FILLED','order_id':'o','detalle':'line one\nseñal final'})
        payload=output.getvalue().encode('utf-8');split=payload.index('ñ'.encode())+1
        with self.path.open('ab') as handle:handle.write(payload[:split])
        rows,ok=self.cache.signals(self.path,{_key(self.signal,signal=True)})
        self.assertTrue(ok)
        self.assertEqual(annotate_signals([self.signal],rows)[0]['execution_evidence']['state'],'SUBMITTED')
        with self.path.open('ab') as handle:handle.write(payload[split:])
        self.assertEqual(self.assert_signal_parity(),'FILL_RECORDED')

    def test_replace_truncate_and_same_size_rewrite_cannot_reuse_old_fills(self):
        self.append(evento='ENTRY_FILLED',order_id='o',estado='FILLED')
        self.assertEqual(self.assert_signal_parity(),'FILL_RECORDED')
        original=self.path.read_bytes()
        self.path.write_bytes(original.replace(b',FILLED,',b',VOIDED,'))
        self.assertEqual(self.assert_signal_parity(),'UNCERTAIN')
        self.path.unlink();self.append(evento='ENTRY_BLOCKED',motivo='AI_THRESHOLD')
        self.assertEqual(self.assert_signal_parity(),'BLOCKED')
        replacement=self.path.with_suffix('.replacement')
        replacement.write_text('invalid,header\n',encoding='utf-8');os.replace(replacement,self.path)
        rows,ok=self.cache.signals(self.path,{_key(self.signal,signal=True)})
        self.assertFalse(ok);self.assertEqual(rows,[])

    def test_latest_paper_quarantine_matches_full_scan_even_with_different_order_ids(self):
        self.append(modo='PAPER',evento='PAPER_OBSERVATION_QUARANTINED',order_id='a',motivo='first')
        self.append(modo='PAPER',evento='PAPER_OBSERVATION_QUARANTINED',order_id='b',motivo='second')
        self.cache.history(self.path)
        self.append(modo='PAPER',evento='PAPER_OBSERVATION_QUARANTINED',order_id='a',motivo='latest')
        history=[{**self.base,'modo':'PAPER','evento':'POSITION_CLOSE','pnl_usd':'1'}]
        _,rows,_=self.cache.history(self.path)
        with self.path.open(encoding='utf-8-sig',newline='') as handle:
            self.assertEqual(annotate_history(history,rows),annotate_history(history,csv.DictReader(handle)))

    def test_summary_updates_for_new_closes_quarantines_and_new_york_day(self):
        import server
        from core.trade_history import HISTORY_FIELDS
        history=Path(self.folder)/'history.csv'
        row={**self.base,'modo':'PAPER','fecha':'2026-11-10','evento':'POSITION_CLOSE','pnl_usd':'3'}
        def close(value,identity):
            new=not history.exists()
            with history.open('a',encoding='utf-8',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=HISTORY_FIELDS)
                if new:writer.writeheader()
                writer.writerow({**row,'pnl_usd':value,'position_id':identity})
        close('3','p')
        with patch.object(server,'_history_path',return_value=str(history)),patch.object(
                server,'_order_audit_path',return_value=str(self.path)),patch.object(
                server,'_ahora_ny_datetime',return_value=datetime(2026,11,10,18,tzinfo=timezone.utc)) as clock:
            self.assertEqual(server._history_summary()['paper_realized_today'],3)
            close('4','other')
            self.assertEqual(server._history_summary()['paper_realized_today'],7)
            self.append(modo='PAPER',evento='PAPER_OBSERVATION_QUARANTINED',
                motivo='CONTROLLED_MAINTENANCE_CLOSE_TEST',
                detalle=json.dumps({'training_eligible':False,'performance_eligible':False}))
            self.assertEqual(server._history_summary()['paper_realized_today'],4)
            clock.return_value=datetime(2026,11,11,18,tzinfo=timezone.utc)
            result=server._history_summary()
            self.assertEqual(result['paper_realized_today'],0)
            self.assertEqual(result['paper_pnl'],4)


if __name__=='__main__':unittest.main()
