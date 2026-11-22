# ab ls --ready exclude + ram_gate hold
- M1 exclude-type/label on --ready + tests/test_ls_ready_exclude.py: done
- M2 ram_gate/board_slots hold (store.hold_reason/record_holds, runner.fill gates) + tests/test_hold_ram_gate.py: done
- M3 CHANGELOG: done
repro: results/ab_ls_ready_hold/repro.sh HOME MIN_FREE (NOADD=1 to skip adding a card; a lifted gate really launches the card)
