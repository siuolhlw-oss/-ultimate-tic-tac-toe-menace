"""以現有 Python 規則逐項核對離線網頁的 JavaScript 引擎；不改動研究資料。"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from uttt.engine import State, legal_moves, play, replay, state_to_dict
from uttt.symmetry import action_groups, canonicalize, transform_move, transform_state

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "web" / "game-atlas" / "engine.js"
BUNDLED_NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
NODE = os.environ.get("UTTT_NODE") or shutil.which("node") or (str(BUNDLED_NODE) if BUNDLED_NODE.is_file() else None)
EXAMPLE = [14, 41, 15, 51, 16, 62, 24, 42, 25, 52, 26, 63, 34, 43, 35, 53, 36]


def groups(state):
    return [{"rep": rep, "members": list(members)} for rep, members in action_groups(state).items()]


def oracle(state, with_groups=True):
    normalized, transform = canonicalize(state)
    row = {"state": state_to_dict(state), "legal": list(legal_moves(state)),
           "canonical": state_to_dict(normalized), "transform": transform}
    if with_groups:
        row["groups"] = groups(state)
    return row


def run_node(body, data=None, args=(), timeout=120):
    source = "const assert = require('node:assert/strict'); const fs=require('node:fs');\n"
    source += "const E=require(" + json.dumps(str(ENGINE)) + ");\n"
    source += "const input=JSON.parse(fs.readFileSync(0,'utf8')||'null');\n" + body
    process = subprocess.run([NODE, "-e", source, *map(str, args)],
                             input=json.dumps(data), encoding="utf-8", capture_output=True,
                             timeout=timeout, cwd=ROOT)
    if process.returncode:
        raise AssertionError(process.stderr)
    return json.loads(process.stdout or "null")


@unittest.skipUnless(NODE, "需要 Node.js；可透過 UTTT_NODE 指定可執行檔。")
class ExplorerEngineTests(unittest.TestCase):
    def test_example_and_full_d4_transitions_match_python(self):
        rows = []
        for state in replay(EXAMPLE):
            row = oracle(state)
            row["transformed"] = []
            for t in range(8):
                transformed = transform_state(state, t)
                row["transformed"].append({
                    "state": state_to_dict(transformed),
                    "legal": list(legal_moves(transformed)),
                    "moves": [[move, transform_move(move, t)] for move in legal_moves(state)],
                })
            rows.append(row)
        summary = run_node("""
          const actual=E.replay(input.moves);
          input.rows.forEach((row,i)=>{
            const state=actual[i]; assert.deepEqual(state,row.state);
            assert.deepEqual(E.legalMoves(state),row.legal);
            const c=E.canonical(state); assert.deepEqual(c.state,row.canonical);
            assert.equal(c.transform,row.transform);
            assert.deepEqual(E.actionGroups(state),row.groups);
            row.transformed.forEach((r,t)=>{
              const s=E.transformState(state,t); assert.deepEqual(s,r.state);
              assert.deepEqual(E.legalMoves(s),r.legal);
              assert.equal(E.canonical(s).key,c.key);
              r.moves.forEach(([move,changed])=>{
                assert.equal(E.transformMove(move,t),changed);
                assert.deepEqual(E.play(s,changed),E.transformState(E.play(state,move),t));
              });
            });
          });
          assert.equal(actual.at(-1).status,'X'); assert.equal(actual.at(-1).turn,'O');
          assert.equal(actual.at(-1).forced,-1); assert.throws(()=>E.play(actual.at(-1),64));
          assert.equal(E.actionGroups(E.initial()).length,15);
          const keys=new Set(E.legalMoves(E.initial()).map(m=>E.canonical(E.play(E.initial(),m)).key));
          assert.equal(keys.size,15);
          console.log(JSON.stringify({states:actual.length}));
        """, {"moves": EXAMPLE, "rows": rows})
        self.assertEqual(summary["states"], 18)

    def test_invalid_moves_closed_boards_and_serialized_state(self):
        won = State("XXXOO...." + "." * 72, "X", 2)
        drawn = State("XOXXOOOXX" + "." * 72, "X", 2)
        closing = State(".XXOO...." + "." * 72, "X", 1)
        all_drawn = State("XOXXOOOXX" * 9)
        run_node("""
          for(const move of [0,7,10,20,100,-11,35.5,'35',true,null,NaN,Infinity])
            assert.throws(()=>E.play(E.initial(),move));
          assert.throws(()=>E.replay([14,51])); assert.throws(()=>E.replay([14,41,14]));
          assert.throws(()=>E.play(input.won,16)); assert.throws(()=>E.play(input.drawn,11));
          for(const state of [input.won,input.drawn]) {
            const child=E.play(state,21); assert.equal(child.forced,0);
            assert(E.legalMoves(child).every(m=>Math.floor(m/10)!==1));
          }
          const closed=E.play(input.closing,11);
          assert.equal(closed.forced,0); assert.equal(closed.local_status[0],'X');
          assert(E.legalMoves(closed).every(m=>Math.floor(m/10)!==1));
          assert.equal(input.allDrawn.status,'draw'); assert.deepEqual(E.legalMoves(input.allDrawn),[]);
          assert.throws(()=>E.play(input.allDrawn,99));
          for(const bad of [{...E.initial(),board:'.'.repeat(80)},
            {...E.initial(),board:'XXXOOO...'+'.'.repeat(72)},
            {...E.initial(),rule_version:'unsupported'}, {...E.initial(),ply:1},
            {...E.initial(),local_status:['X',...Array(8).fill('.')]},
            {...E.initial(),forced:-1}, {...input.won,forced:1}]) assert.throws(()=>E.legalMoves(bad));
          for(const t of [-1,8,true,1.5,'1']) assert.throws(()=>E.transformMove(35,t));
          assert.equal(E.transformMove(35,1),95); assert.equal(E.transformMove(31,1),93);
          console.log('null');
        """, {"won": state_to_dict(won), "drawn": state_to_dict(drawn),
              "closing": state_to_dict(closing), "allDrawn": state_to_dict(all_drawn)})

    def test_complete_state_and_fixed_history_symmetry(self):
        state = play(State(), 14)
        different_forced = State(state.board, state.turn, 5)
        histories = [[transform_move(m, t) for m in EXAMPLE] for t in range(8)]
        expected = min("".join(f"{move:02d}" for move in history) for history in histories)
        run_node("""
          assert.notEqual(E.canonical(input.state).key,E.canonical(input.changed).key);
          assert.notEqual(E.canonical(E.initial()).key,E.canonical({...E.initial(),turn:'O'}).key);
          assert.notEqual(E.canonical(E.initial()).key,E.canonical({...E.initial(),rule_version:'future'}).key);
          for(const h of input.histories) assert.equal(E.canonicalHistory(h),input.expected);
          assert.equal(E.canonicalHistory([]),'');
          // 各小盤獨立旋轉不能當作全盤 D4；本例兩棋之相對配置已改變。
          const a=E.replay([14,41]).at(-1);
          let board=a.board.split(''); board[3]='.'; board[1]='X';
          const independentlyRotated={board:board.join(''),turn:a.turn,forced:a.forced,rule_version:a.rule_version};
          assert.notEqual(E.canonical(a).key,E.canonical(independentlyRotated).key);
          console.log('null');
        """, {"state": state_to_dict(state), "changed": state_to_dict(different_forced),
              "histories": histories, "expected": expected})

    def test_all_1000_training_games_against_python_oracle(self):
        database = ROOT / "data/selfplay-1000/training.sqlite3"
        if not database.is_file():
            self.skipTest("完整對照需本機的 1,000 局訓練資料；小型規則測試不需此檔。")
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            # 精確鎖定已完成的 pilot 批次，不納入之後的真人 PK 或其他對弈。
            games = conn.execute("""
                SELECT g.game_id FROM games g JOIN pilot_batches b ON b.run_id=g.run_id
                WHERE g.status='finished' AND g.mode='train'
                ORDER BY b.batch_index,g.created_at,g.game_id
            """).fetchall()
            self.assertEqual(len(games), 1000)
            with tempfile.TemporaryDirectory(prefix="uttt-js-oracle-") as temp:
                path = Path(temp) / "oracle.jsonl"
                plies = 0
                with path.open("w", encoding="utf-8") as out:
                    for (game_id,) in games:
                        moves = [row[0] for row in conn.execute(
                            "SELECT move_code FROM moves WHERE game_id=? ORDER BY ply", (game_id,))]
                        states = replay(moves)
                        row = {"id": game_id, "moves": moves,
                               "states": [oracle(state, i % 20 == 0 or i == len(moves))
                                          for i, state in enumerate(states)],
                               "history": min("".join(f"{transform_move(m,t):02d}" for m in moves)
                                              for t in range(8))}
                        out.write(json.dumps(row, separators=(",", ":")) + "\n")
                        plies += len(moves)
                result = run_node("""
                  (async()=>{
                    const lines=require('node:readline').createInterface({input:fs.createReadStream(process.argv[1]),crlfDelay:Infinity});
                    let games=0,moves=0,states=0,groupChecks=0;
                    for await(const line of lines) {
                      const row=JSON.parse(line), actual=E.replay(row.moves);
                      assert.equal(actual.length,row.states.length,row.id);
                      row.states.forEach((expected,i)=>{
                        const state=actual[i], label=row.id+' ply '+i;
                        assert.deepEqual(state,expected.state,label);
                        assert.deepEqual(E.legalMoves(state),expected.legal,label);
                        const c=E.canonical(state); assert.deepEqual(c.state,expected.canonical,label);
                        assert.equal(c.transform,expected.transform,label);
                        assert.equal(c.key,c.state.board+'|'+c.state.turn+'|'+c.state.forced+'|'+c.state.rule_version);
                        if(expected.groups) {assert.deepEqual(E.actionGroups(state),expected.groups,label);groupChecks++;}
                        states++;
                      });
                      assert.equal(E.canonicalHistory(row.moves),row.history,row.id);
                      assert.notEqual(actual.at(-1).status,'active',row.id);
                      games++;moves+=row.moves.length;
                    }
                    console.log(JSON.stringify({games,moves,states,groupChecks}));
                  })().catch(error=>{console.error(error);process.exitCode=1;});
                """, args=(path,), timeout=180)
                self.assertEqual(result["games"], 1000)
                self.assertEqual(result["moves"], plies)
                self.assertEqual(result["states"], plies + 1000)
                self.assertGreater(result["groupChecks"], 2000)
                print("\n網頁引擎全棋譜核對：" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
