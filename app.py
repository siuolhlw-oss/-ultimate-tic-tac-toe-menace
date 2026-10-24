"""超級井字棋 MENACE 的本機教學介面。

執行：python -m streamlit run app.py
資料庫由 UTTT_DB_PATH 指定；session 只保存 ID、局面及展示結果。
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import uuid

import streamlit as st

from uttt.engine import State, legal_moves, play, replay, state_from_dict, state_to_dict
from uttt.service import ResearchService
from uttt.stream_export import export_archive
from uttt.symmetry import canonicalize, state_key


def model_label(model: dict) -> str:
    return f"{model['name']} · {model['model_id']} · v{model['version']}"


def player_label(player: str, models: list[dict]) -> str:
    if player == "human":
        return "人類"
    if player == "random":
        return "合法均勻隨機"
    return next((model_label(m) for m in models if m["model_id"] == player), player)


def game_label(game: dict) -> str:
    status = {"active": "未完", "finished": "完成", "abandoned": "已中止"}.get(
        game["status"], game["status"])
    return f"{game['game_id']} · {status} · {game['ply']} 手"


def notice(message: str) -> None:
    st.session_state["notice"] = message
    st.rerun()


def describe_state(state: State) -> None:
    if state.status == "draw":
        st.info("和局：沒有大格三連線，所有小盤均已封閉。")
    elif state.status != "active":
        st.success(f"{state.status} 大格三連線，獲勝！")
    else:
        target = f"大格 {state.forced}" if state.forced else "任一未封閉小盤"
        st.write(f"第 {state.ply + 1} 手，輪到 **{state.turn}**。本手可下：**{target}**。")


def board(state: State, prefix: str, interactive: bool = False) -> int | None:
    """81 格原生按鈕。即使唯讀也呈現合法位置，互動時另檢查 actor。"""
    allowed = set(legal_moves(state))
    clicked = None
    labels = {".": "未封閉", "X": "X 取得 · 封閉", "O": "O 取得 · 封閉", "D": "和局 · 封閉"}
    for big_row in range(3):
        columns = st.columns(3, gap="small")
        for big_col, column in enumerate(columns):
            big = big_row * 3 + big_col + 1
            with column, st.container(border=True):
                st.caption(f"大格 {big}｜{labels[state.local_status[big - 1]]}")
                for small_row in range(3):
                    cells = st.columns(3, gap="small")
                    for small_col, cell in enumerate(cells):
                        small = small_row * 3 + small_col + 1
                        code = big * 10 + small
                        mark = state.board[(big - 1) * 9 + small - 1]
                        with cell:
                            if st.button(mark if mark != "." else str(code),
                                         key=f"{prefix}_{code}", width="stretch",
                                         type="primary" if code in allowed else "secondary",
                                         disabled=not interactive or code not in allowed,
                                         help=f"位置 {code}：大格 {big}、小格 {small}"):
                                clicked = code
    return clicked


def show_moves(moves: list[dict]) -> None:
    if not moves:
        st.caption("尚未落子。")
        return
    rows = []
    for move in moves:
        probability = move.get("probability")
        rows.append({"手數": move["ply"], "行動方": move["actor"],
                     "原始位置": move["move_code"], "抽選方式": move.get("branch"),
                     "抽選前每格珠數": move.get("weight_before_choice"),
                     "本手選取機率": None if probability is None else f"{probability:.2%}"})
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("選取機率描述這一手如何被抽中，不是獲勝機率。原始位置依當時畫面記錄。")


def play_page(service: ResearchService, models: list[dict]) -> None:
    st.header("對弈")
    st.caption("大小格皆按左上到右下編為 1–9；35 是大格 3 的小格 5。亮色空格為合法位置。")
    players = ["human", "random"] + [m["model_id"] for m in models]
    mode_labels = {"eval": "PK 評估（凍結模型）", "train": "訓練（賽後學習）", "demo": "教學示範（不學習）"}
    mode_notes = {
        "eval": "PK 評估：使用開局時的模型，不學習、不增加探索；依珠數抽選，未見局面在合法格中均勻抽選。棋譜照常保存。",
        "train": "訓練：90% 珠數抽選、10% 合法格探索；整局結束後才更新珠數。棋譜照常保存，中止不計勝負。",
        "demo": "教學示範：不學習、不增加探索，棋譜標記為示範，與正式 PK 分開保存。",
    }
    with st.expander("開始新對局", expanded="current_game_id" not in st.session_state):
        default_mode = os.environ.get("UTTT_PLAY_MODE", "eval")
        if default_mode not in mode_labels:
            default_mode = "eval"
        mode = st.radio("對局模式", list(mode_labels), index=list(mode_labels).index(default_mode),
                        format_func=mode_labels.get, key="new_game_mode")
        st.caption(mode_notes[mode])
        with st.form("new_game"):
            first, second = st.columns(2)
            x_player = first.selectbox("X（先手）", players, format_func=lambda p: player_label(p, models))
            o_player = second.selectbox("O（後手）", players, index=min(2, len(players) - 1),
                                        format_func=lambda p: player_label(p, models))
            seed = st.number_input("對局隨機種子", min_value=0, value=0, step=1)
            if st.form_submit_button("建立對局", type="primary"):
                st.session_state["current_game_id"] = service.new_game(
                    x_player, o_player, seed=int(seed), mode=mode, is_example=mode == "demo")
                st.rerun()
    games = service.list_games(limit=500)
    unfinished = [g for g in games if g["status"] == "active"]
    if unfinished:
        with st.expander(f"續玩資料庫中的未完對局（{len(unfinished)} 局）"):
            choices = {g["game_id"]: g for g in unfinished}
            selected = st.selectbox("未完對局", list(choices), format_func=lambda gid: game_label(choices[gid]))
            if st.button("載入對局"):
                st.session_state["current_game_id"] = selected
                st.rerun()
    game_id = st.session_state.get("current_game_id")
    if not game_id:
        st.info("先建立或載入一局。AI 模型可在左側新增。")
        return
    game = service.get_game(game_id)
    state = service.state(game_id)
    st.caption(f"對局 {game_id}｜X：{player_label(game['x_player'], models)}｜O：{player_label(game['o_player'], models)}")
    st.caption(f"本局模式：{mode_labels[game['mode']]}。{mode_notes[game['mode']]}")
    is_active = game["status"] == "active"
    player = game["x_player"] if state.turn == "X" else game["o_player"]
    human_ai = (game["x_player"] == "human") != (game["o_player"] == "human")
    auto_reply = st.checkbox("人機對弈時自動應手", value=True)
    if is_active and human_ai and auto_reply and player != "human":
        service.step(game_id, expected_ply=game["ply"])
        st.rerun()
    if game["status"] == "abandoned":
        st.warning("這局已中止，保留棋譜，不再落子。")
    describe_state(state)
    # 手數納入 widget key：舊畫面的點擊不可被套用到另一手或另一局。
    move = board(state, f"game_{game_id}_{game['ply']}", interactive=is_active and player == "human")
    if move is not None:
        service.step(game_id, move, expected_ply=game["ply"])
        st.rerun()
    left, right = st.columns(2)
    if left.button("AI／隨機走一步", key=f"step_{game_id}_{game['ply']}",
                   disabled=not is_active or player == "human", width="stretch"):
        service.step(game_id, expected_ply=game["ply"])
        st.rerun()
    if right.button("中止此局（保留棋譜）", disabled=not is_active, width="stretch"):
        service.abandon(game_id)
        notice("對局已中止；棋譜仍可回放及匯出。")
    with st.expander("本局落子順序"):
        show_moves(service.moves(game_id))


def experiments_page(service: ResearchService, models: list[dict]) -> None:
    st.header("訓練與評估")
    st.info("每批局數保持小量，完成後即存入資料庫。短期勝率變動不能單獨證明收斂或最佳策略。")
    by_id = {m["model_id"]: m for m in models}
    ids = list(by_id)
    if not ids:
        st.warning("請先在左側建立 AI 模型。")
        return
    label = lambda mid: model_label(by_id[mid])
    st.subheader("兩個 AI 自我對弈")
    with st.form("train"):
        cols = st.columns(2)
        model_a = cols[0].selectbox("模型 A", ids, format_func=label)
        model_b = cols[1].selectbox("模型 B", ids, index=min(1, len(ids) - 1), format_func=label)
        games = st.number_input("本批訓練局數", min_value=1, max_value=200, value=10, step=1)
        seed = st.number_input("批次對局種子（不重設模型亂數）", min_value=0, value=0, step=1)
        st.caption("90% 珠數加權抽選、10% 合法位置均勻探索；每局完成後才更新。請以不同模型比較學習。")
        st.caption("批次種子不重設模型既有亂數狀態。獨立重複實驗須新建模型 A、B，並使用不同的初始模型種子。")
        start = st.form_submit_button("開始這一批訓練", type="primary")
    if start:
        if model_a == model_b:
            st.error("請選兩個不同模型；可在左側再建立一個從零開始的 AI。")
        else:
            progress = st.progress(0.0, text="準備訓練")
            result = service.train(model_a, model_b, games=int(games), seed=int(seed),
                                   progress=lambda done, total: progress.progress(done / total, text=f"訓練 {done}/{total} 局"))
            st.session_state["last_training"] = result
            notice(f"本批 {result['games']} 局訓練完成。")
    if "last_training" in st.session_state:
        st.json(st.session_state["last_training"], expanded=True)
    st.subheader("凍結評估：對抗合法均勻隨機")
    with st.form("evaluate"):
        model_id = st.selectbox("待評估模型", ids, format_func=label)
        eval_games = st.number_input("評估局數（建議偶數以平衡先後手）", min_value=2, max_value=200, value=20, step=2)
        eval_seed = st.number_input("評估種子", min_value=0, value=0, step=1)
        st.caption("凍結檢查點，交替先後手。關閉額外探索，仍按珠數抽選；未見局面均勻合法抽選，不更新模型。")
        evaluate = st.form_submit_button("開始凍結評估")
    if evaluate:
        progress = st.progress(0.0, text="準備評估")
        result = service.evaluate(model_id, games=int(eval_games), seed=int(eval_seed),
                                  progress=lambda done, total: progress.progress(done / total, text=f"評估 {done}/{total} 局"))
        st.session_state["last_evaluation"] = result
        notice(f"本批 {result['games']} 局評估完成，訓練模型保持不變。")
    if "last_evaluation" in st.session_state:
        result = st.session_state["last_evaluation"]
        cols = st.columns(4)
        for col, label_text, key in zip(cols, ("勝", "和", "負", "得分率"), ("wins", "draws", "losses", "score")):
            col.metric(label_text, f"{result[key]:.1%}" if key == "score" else result[key])
        st.caption("得分率 =（勝局數 + 0.5 × 和局數）÷ 評估局數。不同種子與樣本數亦須保留。")
        st.json(result, expanded=False)


def node_name(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def graph_source(info: dict, count: int) -> str:
    state = info["canonical_state"]
    status = "已建立盒子" if info["known"] else "未見局面（僅預覽）"
    root = f"{node_name(info['state_key'])}\n{state.ply} 手／{state.turn} 行動／指定 {state.forced}\n{status}"
    root_id = "n" + hashlib.sha256(info["state_key"].encode("utf-8")).hexdigest()
    lines = ["digraph G {", 'rankdir=LR; node [shape=box];',
             f"{root_id} [label={json.dumps(root, ensure_ascii=False)}];"]
    emitted = {root_id}
    for group in info["groups"][:count]:
        # 在標準方向合法落子，再正規化後繼；同一子局面使用同一節點。
        child, _ = canonicalize(play(state, group["rep"]))
        key = state_key(child)
        child_id = "n" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        if child_id not in emitted:
            target = "自由" if child.forced == 0 else "終局" if child.forced == -1 else str(child.forced)
            result = {"active": "未終局", "draw": "和局", "X": "X 勝", "O": "O 勝"}[child.status]
            label = f"{node_name(key)}\n{child.ply} 手／{child.turn} 行動／指定 {target}\n{result}（後繼預覽）"
            lines.append(f"{child_id} [label={json.dumps(label, ensure_ascii=False)}];")
            emitted.add(child_id)
        members = ",".join(map(str, group["members"]))
        beads = "無權重（均勻）" if group["weight"] is None else f"每格珠數 {group['weight']}"
        visits = group.get("visits")
        visit_label = "選取次數未提供" if visits is None else f"本模型歷來選取 {visits} 次"
        label = f"動作類 {members}\n{beads}／類機率 {group['probability']:.2%}\n{visit_label}"
        lines.append(f'{root_id} -> {child_id} [style=dashed,label={json.dumps(label, ensure_ascii=False)}];')
    return "\n".join(lines + ["}"])


def strategy_page(service: ResearchService, models: list[dict]) -> None:
    st.header("策略探索")
    st.caption("此頁只讀。預覽下法、切換局面及查看珠數均不建立盒子，也不更新模型。")
    if not models:
        st.info("請先建立模型。")
        return
    by_id = {m["model_id"]: m for m in models}
    model_id = st.selectbox("查看模型", list(by_id), format_func=lambda mid: model_label(by_id[mid]))
    checkpoints = service.list_checkpoints(model_id)
    checkpoint_map = {c["checkpoint_id"]: c for c in checkpoints}
    checkpoint_id = st.selectbox("資料版本", [None] + list(checkpoint_map),
                                  format_func=lambda cid: "目前訓練模型" if cid is None else f"檢查點 {cid} · v{checkpoint_map[cid]['version']}")
    training = st.checkbox("顯示訓練抽選機率（含 10% 探索）", value=True)
    origins = ["初始空盤"]
    if st.session_state.get("current_game_id"):
        origins.append("目前對局")
    origins.append("模型已記錄局面")
    origin = st.selectbox("載入起點", origins)
    selected_key = None
    if origin == "模型已記錄局面":
        states = service.list_states(model_id, limit=200, checkpoint_id=checkpoint_id)
        if states:
            state_map = {s["state_key"]: s for s in states}
            selected_key = st.selectbox("局面（最多顯示 200 筆）", list(state_map),
                                        format_func=lambda key: f"{state_map[key]['ply']} 手 · {node_name(key)}")
        else:
            st.info("這個模型尚未記錄局面，仍可從空盤預覽。")
    if st.button("載入起點並清除預覽路徑"):
        if origin == "目前對局":
            state = service.state(st.session_state["current_game_id"])
        elif selected_key:
            state = state_from_dict(json.loads(selected_key))
        else:
            state = State()
        st.session_state["preview_history"] = [state_to_dict(state)]
        st.rerun()
    history = st.session_state.setdefault("preview_history", [state_to_dict(State())])
    state = state_from_dict(history[-1])
    info = service.inspect(state, model_id, training=training, checkpoint_id=checkpoint_id)
    describe_state(state)
    board(state, "preview")
    st.write(f"標準化節點 `{node_name(info['state_key'])}`｜模型版本 {info['model_version']}｜D4 轉換 {info['transform']}")
    if info["known"]:
        st.info("模型已有此盒子。")
    elif training:
        st.info("模型尚未見過此局面：以每合法格初始 3 珠作只讀預覽，不代表訓練已探索。")
    else:
        st.info("模型尚未見過此局面：評估採合法位置均勻抽選，沒有已學得權重，也不建立盒子。")
    actions = info["actions"]
    if actions:
        rows = [{"畫面位置": a["move"], "標準位置": a["canonical_move"],
                 "同構類代表": a["group_rep"], "同類格數": a["group_size"],
                 "每格珠數": a["weight"], "本格選取機率": f"{a['probability']:.2%}"} for a in actions]
        st.dataframe(rows, hide_index=True, width="stretch")
        st.caption("每格權重共享於同構類；類加權為「類大小 × 每格珠數」。機率是選取機率，不是勝率。")
        cols = st.columns([2, 1, 1])
        move = cols[0].selectbox("選合法畫面位置，預覽下一局面", [a["move"] for a in actions])
        if cols[1].button("預覽下一局面"):
            history.append(state_to_dict(play(state, move)))
            st.rerun()
        if cols[2].button("回上一步", disabled=len(history) < 2):
            history.pop()
            st.rerun()
        max_count = min(20, len(info["groups"]))
        count = st.number_input("圖中顯示的動作類數", min_value=1, max_value=max_count,
                                value=min(8, max_count), step=1)
        st.caption(f"共 {len(info['groups'])} 個合法動作類，圖中顯示前 {int(count)} 類；相同標準子局面合併為同一節點。")
        st.caption("圖為目前節點的合法後繼預覽；虛線不是已訓練的路徑。動作採標準座標。")
        if checkpoint_id:
            st.caption("檢查點只提供當時的權重快照，未提供該快照的走訪次數。")
        else:
            st.caption("次數是本模型歷來的非示範訓練選取紀錄。資料庫的轉移索引也可能來自其他模型或示範，不等於本模型的訓練次數。")
        dot = graph_source(info, int(count))
        try:
            st.graphviz_chart(dot, width="stretch")
        except ImportError:
            st.code(dot, language="dot")
    else:
        st.caption("已終局，沒有合法後繼。")
        if st.button("回上一步", disabled=len(history) < 2):
            history.pop()
            st.rerun()
    with st.expander("標準化局面與完整狀態鍵"):
        st.json(state_to_dict(info["canonical_state"]))
        st.code(info["state_key"], language="json")


def replay_page(service: ResearchService) -> None:
    st.header("棋譜回放")
    games = service.list_games(limit=500)
    if not games:
        st.info("尚無對局紀錄。")
        return
    by_id = {g["game_id"]: g for g in games}
    game_id = st.selectbox("選擇對局（最近 500 局）", list(by_id), format_func=lambda gid: game_label(by_id[gid]))
    moves = service.moves(game_id)
    states = replay([m["move_code"] for m in moves])
    ply = st.slider("回放至第幾手", 0, len(moves), 0, key=f"replay_{game_id}") if moves else 0
    state = states[ply]
    if ply:
        st.write(f"上一手：{moves[ply - 1]['actor']} 下在 **{moves[ply - 1]['move_code']}**。")
    describe_state(state)
    board(state, "replay")
    show_moves(moves[:ply])
    with st.expander("對局資訊"):
        st.json(by_id[game_id])


def models_page(service: ResearchService, models: list[dict]) -> None:
    st.header("模型與匯出")
    if models:
        st.dataframe([{"模型名稱": m["name"], "模型 ID": m["model_id"],
                       "版本": m["version"], "完成訓練局數": m["trained_games"]} for m in models],
                     hide_index=True, width="stretch")
        by_id = {m["model_id"]: m for m in models}
        model_id = st.selectbox("檢查點的模型", list(by_id), format_func=lambda mid: model_label(by_id[mid]))
        if st.button("保存目前模型檢查點"):
            checkpoint_id = service.save_checkpoint(model_id)
            notice(f"已保存檢查點 {checkpoint_id}。")
        checkpoints = service.list_checkpoints(model_id)
        if checkpoints:
            by_checkpoint = {c["checkpoint_id"]: c for c in checkpoints}
            with st.form("restore"):
                checkpoint_id = st.selectbox("從哪個檢查點建立新模型", list(by_checkpoint),
                                              format_func=lambda cid: f"{cid} · v{by_checkpoint[cid]['version']} · {by_checkpoint[cid]['created_at']}")
                name = st.text_input("新模型名稱（可留白）")
                if st.form_submit_button("由檢查點另建模型"):
                    new_id = service.restore_checkpoint(checkpoint_id, name=name.strip() or None)
                    notice(f"已另建模型 {new_id}，原模型保留。")
    else:
        st.info("可在左側建立第一個模型。")
    st.subheader("匯出研究資料")
    is_pilot = service.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_experiments'"
    ).fetchone() is not None
    if is_pilot:
        st.caption("批次實驗採完整匯出，保留所有棋譜、模型與批次對應紀錄，以便追溯訓練過程。")
        run_id = ""
    else:
        st.caption("ZIP 包含 CSV 與 JSON。保留原始棋譜、設定及模型相關紀錄；可指定訓練／評估 run_id，留白匯出全部。")
        run_id = st.text_input("run_id（可留白）").strip()
    if st.button("準備 ZIP 檔"):
        filename = f"uttt_research_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:12]}.zip"
        if run_id:
            st.session_state["export_payload"] = service.export_data(run_id=run_id)
        else:
            # 先逐列寫入磁碟，下載時只載入壓縮完成的 ZIP。
            destination = Path(service.repo.path).resolve().parent / "exports" / filename
            with st.spinner("正在準備完整資料快照……"):
                export_archive(service, destination)
                st.session_state["export_payload"] = destination.read_bytes()
        st.session_state["export_scope"] = run_id
        st.session_state["export_filename"] = filename
    if "export_payload" in st.session_state and st.session_state.get("export_scope") == run_id:
        st.download_button("下載 CSV＋JSON ZIP", data=st.session_state["export_payload"],
                           file_name=st.session_state["export_filename"], mime="application/zip")
        st.caption("下載的是按「準備」時的資料快照；有新增對局時，請重新準備 ZIP。")


def main() -> None:
    st.set_page_config(page_title="超級井字棋 MENACE 研究", page_icon="⭕", layout="wide")
    st.title("超級井字棋 MENACE 研究")
    st.caption("封閉小盤版 · 大格三連線立即獲勝 · 本機研究工具")
    db_path = os.environ.get("UTTT_DB_PATH", "data/research.sqlite3")
    try:
        with ResearchService(db_path=db_path) as service:
            models = service.list_models()
            with st.sidebar:
                section = st.radio("工作區", ["對弈", "訓練與評估", "策略探索", "棋譜回放", "模型與匯出"])
                st.divider()
                with st.form("create_model"):
                    st.subheader("從零建立 AI")
                    name = st.text_input("模型名稱", value=f"AI-{len(models) + 1}")
                    seed = st.number_input("模型種子", min_value=0, value=len(models), step=1)
                    if st.form_submit_button("建立模型"):
                        if not name.strip():
                            st.error("請填模型名稱。")
                        else:
                            new_id = service.create_model(name.strip(), seed=int(seed))
                            notice(f"已建立模型 {name.strip()}（{new_id}）。")
                st.caption(f"目前 {len(models)} 個模型。資料會寫入本機 SQLite。")
            message = st.session_state.pop("notice", None)
            if message:
                st.success(message)
            pages = {"對弈": lambda: play_page(service, models),
                     "訓練與評估": lambda: experiments_page(service, models),
                     "策略探索": lambda: strategy_page(service, models),
                     "棋譜回放": lambda: replay_page(service),
                     "模型與匯出": lambda: models_page(service, models)}
            pages[section]()
    except Exception as exc:
        st.error(f"操作未完成：{exc}")
        st.caption("資料庫中的已完成紀錄仍會保留。若是未完對局，可重新載入後繼續。")


if __name__ == "__main__":
    main()
