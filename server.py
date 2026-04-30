from __future__ import annotations

import random
import string
import threading
from copy import deepcopy
from typing import Dict, List, Optional

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "sutda-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

START_STACK = 1_000_000  # 각자 시작칩
ANTE = 10_000
MAX_PLAYERS = 5

lock = threading.Lock()
rooms: Dict[str, dict] = {}
sid_to_room: Dict[str, str] = {}


def gen_room_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choice(alphabet) for _ in range(5))
        if code not in rooms:
            return code


def unique_name(room: dict, name: str) -> str:
    existing = {p["name"] for p in room["players"]}
    if name not in existing:
        return name
    i = 2
    while f"{name}{i}" in existing:
        i += 1
    return f"{name}{i}"


def make_card(rank: int, kwang: bool = False, ten_variant: bool = False) -> dict:
    label = f"{rank}{'광' if kwang else ''}"
    return {
        "rank": rank,
        "kwang": kwang,
        "ten_variant": ten_variant,
        "label": label,
        "id": f"{rank}{'K' if kwang else ''}{'T' if ten_variant else ''}",
    }


def make_deck() -> List[dict]:
    """
    간단한 섯다 덱.
    - 광: 1광, 3광, 8광
    - 숫자 카드: 1~10 각 2장
    - 멍사구 판정용: 4와 9에 각각 '열자리' 변종 1장씩 포함
    """
    deck: List[dict] = []

    # 광
    deck.append(make_card(1, kwang=True))
    deck.append(make_card(3, kwang=True))
    deck.append(make_card(8, kwang=True))

    # 숫자 카드 (2장씩)
    for r in range(1, 11):
        if r in (4, 9):
            deck.append(make_card(r, ten_variant=True))
            deck.append(make_card(r, ten_variant=False))
        else:
            deck.append(make_card(r))
            deck.append(make_card(r))
    random.shuffle(deck)
    return deck


def draw(room: dict) -> dict:
    if not room["deck"]:
        room["deck"] = make_deck()
    return room["deck"].pop()


def is_active(p: dict) -> bool:
    return (not p["out"]) and (not p["folded"])


def active_players(room: dict) -> List[dict]:
    return [p for p in room["players"] if is_active(p)]


def first_active_index(room: dict) -> Optional[int]:
    for i, p in enumerate(room["players"]):
        if is_active(p):
            return i
    return None


def next_active_index(room: dict, start_idx: int) -> Optional[int]:
    n = len(room["players"])
    if n == 0:
        return None
    for step in range(1, n + 1):
        idx = (start_idx + step) % n
        if is_active(room["players"][idx]):
            return idx
    return None


def is_ttaeng(name: str) -> bool:
    return name.endswith("땡") and name != "땡잡이" and name != "광땡" and "광땡" not in name


def is_gwang_ttang(name: str) -> bool:
    return name in {"13광땡", "18광땡", "38광땡", "광땡"}


def rank_two(c1: dict, c2: dict) -> dict:
    n1, n2 = c1["rank"], c2["rank"]
    low, high = sorted((n1, n2))

    # 멍사구: 열자리 4 + 열자리 9만
    if {low, high} == {4, 9} and c1["ten_variant"] and c2["ten_variant"]:
        return {"name": "멍사구", "value": 9000, "replay": True}

    # 4/9 재경기
    if {low, high} == {4, 9}:
        return {"name": "49", "value": 8900, "replay": True}

    # 광땡
    if c1["kwang"] and c2["kwang"]:
        if {low, high} == {1, 3}:
            return {"name": "13광땡", "value": 7800, "replay": False}
        if {low, high} == {1, 8}:
            return {"name": "18광땡", "value": 7900, "replay": False}
        if {low, high} == {3, 8}:
            return {"name": "38광땡", "value": 8000, "replay": False}
        return {"name": "광땡", "value": 7700, "replay": False}

    # 땡
    if n1 == n2:
        name = "장땡" if n1 == 10 else f"{n1}땡"
        # 10땡이 가장 강하게 되도록 value를 크게
        return {"name": name, "value": 7000 + n1, "replay": False}

    # 특수 족보
    special = {
        (1, 2): ("알리", 6000),
        (1, 4): ("독사", 5900),
        (1, 9): ("구삥", 5800),
        (1, 10): ("장삥", 5700),
        (4, 10): ("장사", 5600),
        (4, 6): ("세륙", 5500),
    }
    if (low, high) in special:
        name, value = special[(low, high)]
        return {"name": name, "value": value, "replay": False}

    # 암행어사: 13/18광땡만 잡고 나머지는 1끗 취급
    if {low, high} == {4, 7}:
        return {"name": "암행어사", "value": 110, "replay": False}

    # 땡잡이: 모든 땡 잡고, 나머지는 망통급
    if {low, high} == {3, 7}:
        return {"name": "땡잡이", "value": 5, "replay": False}

    total = (n1 + n2) % 10
    if total == 0:
        return {"name": "망통", "value": 10, "replay": False}
    if total == 9:
        return {"name": "갑오", "value": 100 + total, "replay": False}
    return {"name": f"{total}끗", "value": 100 + total, "replay": False}


def best_hand(hand: List[dict]) -> dict:
    if len(hand) < 2:
        return {"name": "-", "value": -1, "replay": False, "chosen": []}

    if len(hand) == 3 and any(c["rank"] == 4 for c in hand) and any(c["rank"] == 9 for c in hand):
        special4 = [c for c in hand if c["rank"] == 4 and c["ten_variant"]]
        special9 = [c for c in hand if c["rank"] == 9 and c["ten_variant"]]
        if special4 and special9:
            return {"name": "멍사구", "value": 9000, "replay": True, "chosen": [special4[0], special9[0]]}

    best = None
    best_pair = []
    for i in range(len(hand)):
        for j in range(i + 1, len(hand)):
            r = rank_two(hand[i], hand[j])
            if best is None or compare_rank(r, best) == 1:
                best = r
                best_pair = [hand[i], hand[j]]
    return {**best, "chosen": best_pair}


def compare_rank(a: dict, b: dict) -> int | str:
    # 재경기 여부는 상위 로직에서 처리
    # 땡잡이: 땡만 잡고, 나머지는 망통급
    if a["name"] == "땡잡이":
        if is_ttaeng(b["name"]):
            return 1
        if b["name"] == "암행어사":
            return 1 if False else -1
        # 땡 외에는 거의 최하
        return -1 if b["value"] > a["value"] else 0

    if b["name"] == "땡잡이":
        if is_ttaeng(a["name"]):
            return -1
        if a["name"] == "암행어사":
            return -1 if False else 1
        return 1 if a["value"] > b["value"] else 0

    # 암행어사: 13/18광땡만 승리, 나머지는 1끗 취급
    if a["name"] == "암행어사":
        if b["name"] in {"13광땡", "18광땡"}:
            return 1
        if b["name"] == "38광땡":
            return -1
        a_value = 110
        b_value = b["value"]
        if a_value > b_value:
            return 1
        if a_value < b_value:
            return -1
        return 0

    if b["name"] == "암행어사":
        if a["name"] in {"13광땡", "18광땡"}:
            return -1
        if a["name"] == "38광땡":
            return 1
        a_value = a["value"]
        b_value = 110
        if a_value > b_value:
            return 1
        if a_value < b_value:
            return -1
        return 0

    if a["value"] > b["value"]:
        return 1
    if a["value"] < b["value"]:
        return -1
    return 0


def ai_limit(name: str) -> int:
    return {
        "38광땡": 200_000_000,
        "18광땡": 180_000_000,
        "13광땡": 170_000_000,
        "광땡": 150_000_000,
        "장땡": 100_000_000,
        "9땡": 80_000_000,
        "8땡": 70_000_000,
        "7땡": 60_000_000,
        "6땡": 50_000_000,
        "5땡": 40_000_000,
        "4땡": 35_000_000,
        "3땡": 30_000_000,
        "2땡": 25_000_000,
        "1땡": 20_000_000,
        "땡잡이": 70_000_000,
        "암행어사": 50_000_000,
        "알리": 30_000_000,
        "독사": 25_000_000,
        "구삥": 22_000_000,
        "장삥": 20_000_000,
        "장사": 18_000_000,
        "세륙": 16_000_000,
        "갑오": 10_000_000,
        "9끗": 7_000_000,
        "8끗": 6_000_000,
        "7끗": 5_000_000,
        "6끗": 4_000_000,
        "5끗": 3_000_000,
        "4끗": 2_000_000,
        "3끗": 1_500_000,
        "2끗": 1_000_000,
        "1끗": 500_000,
        "망통": 0,
        "49": 0,
        "멍사구": 0,
    }.get(name, 3_000_000)


def ai_decision(room: dict, player: dict) -> str:
    r = best_hand(player["hand"])
    cap = ai_limit(r["name"])
    call_cost = room["current_to_call"]
    half_cost = call_cost + room["pot"] // 2

    if player["stack"] < call_cost:
        return "die"

    if r["name"] in {"49", "멍사구"}:
        return "call"

    if half_cost <= cap:
        # 강한 족보는 밀고, 약한 족보는 가끔만 하프
        if r["value"] >= 7700:
            return "half" if random.random() < 0.75 else "call"
        if r["value"] >= 7000:
            return "half" if random.random() < 0.50 else "call"
        if r["value"] >= 6000:
            return "half" if random.random() < 0.35 else "call"
        if r["value"] >= 1000:
            return "half" if random.random() < 0.18 else "call"

    if call_cost > max(100_000, player["stack"] * 0.25):
        return "die"
    if random.random() < 0.08 and half_cost <= cap:
        return "half"
    return "call"


def room_public_state(room: dict, sid: Optional[str]) -> dict:
    viewer_name = None
    for p in room["players"]:
        if p.get("sid") == sid:
            viewer_name = p["name"]
            break

    reveal_all = room["phase"] in {"showdown", "finished"}
    turn_name = None
    current = current_player(room)
    if current:
        turn_name = current["name"]

    players_state = []
    for p in room["players"]:
        show_cards = reveal_all or p["name"] == viewer_name or p["out"] or p["folded"]
        hand = deepcopy(p["hand"]) if show_cards else [{"hidden": True} for _ in p["hand"]]
        players_state.append({
            "name": p["name"],
            "human": p["human"],
            "stack": p["stack"],
            "out": p["out"],
            "folded": p["folded"],
            "last_action": p["last_action"],
            "hand": hand,
            "rank": p.get("rank"),
            "is_viewer": p["name"] == viewer_name,
            "show_cards": show_cards,
        })

    return {
        "room_code": room["code"],
        "mode": room["mode"],
        "phase": room["phase"],
        "round_no": room["round_no"],
        "pot": room["pot"],
        "current_to_call": room["current_to_call"],
        "half_count": room["half_count"],
        "third_dealt": room["third_dealt"],
        "host_sid": room["host_sid"],
        "host_name": room["host_name"],
        "my_name": viewer_name,
        "my_turn": (viewer_name is not None and turn_name == viewer_name and room["phase"] == "betting"),
        "current_turn_name": turn_name,
        "players": players_state,
        "log": room["log"][-12:],
        "started": room["started"],
        "can_start": (room["phase"] == "lobby" and len([p for p in room["players"] if not p["out"]]) >= 2),
        "is_host": sid == room["host_sid"],
        "is_creator": sid == room["host_sid"],
        "is_finished": room["phase"] == "finished",
    }


def push_state(room_code: str) -> None:
    room = rooms.get(room_code)
    if not room:
        return
    human_sids = [p["sid"] for p in room["players"] if p["human"] and p.get("sid")]
    for sid in human_sids:
        socketio.emit("state", room_public_state(room, sid), to=sid)


def log(room: dict, msg: str) -> None:
    room["log"].append(msg)
    if len(room["log"]) > 100:
        room["log"] = room["log"][-100:]


def current_player(room: dict) -> Optional[dict]:
    if room["turn_index"] is None:
        return None
    n = len(room["players"])
    if n == 0:
        return None
    idx = room["turn_index"] % n
    if not room["players"]:
        return None
    # current turn should always point to active player; fallback to next active
    for _ in range(n):
        p = room["players"][idx]
        if is_active(p) and p["name"] in room["pending"]:
            room["turn_index"] = idx
            return p
        idx = (idx + 1) % n
    return None


def advance_turn(room: dict) -> None:
    if not room["players"]:
        room["turn_index"] = None
        return
    cur = current_player(room)
    if cur is None:
        room["turn_index"] = first_active_index(room)
        return
    nxt = next_active_index(room, room["turn_index"])
    room["turn_index"] = nxt


def start_betting_hand(room: dict, replay: bool = False) -> None:
    active = [p for p in room["players"] if not p["out"]]
    if len(active) < 2:
        room["phase"] = "finished"
        log(room, "게임 종료")
        push_state(room["code"])
        return

    room["phase"] = "betting"
    room["current_to_call"] = ANTE
    room["half_count"] = 0
    room["third_dealt"] = False
    room["deck"] = make_deck()
    room["pending"] = set()
    room["turn_index"] = None

    if not replay:
        room["pot"] = 0
        for p in active:
            p["folded"] = False
            p["last_action"] = ""
            p["rank"] = None
            p["chosen"] = []
            p["hand"] = []
            if p["stack"] < ANTE:
                p["out"] = True
                log(room, f"{p['name']} 칩 부족으로 탈락")
                continue
            p["stack"] -= ANTE
            room["pot"] += ANTE
        active = [p for p in room["players"] if not p["out"]]
        for p in active:
            p["hand"] = [draw(room), draw(room)]
            room["pending"].add(p["name"])
    else:
        # 재경기: 판돈 유지, 2장으로 리셋, 다이하지 않은 사람만 계속
        for p in active:
            p["folded"] = False
            p["last_action"] = ""
            p["rank"] = None
            p["chosen"] = []
            p["hand"] = [draw(room), draw(room)]
            room["pending"].add(p["name"])

    room["turn_index"] = first_active_index(room)
    log(room, "🔥 2장 섯다 시작" if replay else f"=== {room['round_no']}판 시작 ===")
    log(room, f"시작금 1만원 / 판돈 {room['pot']:,}원")
    push_state(room["code"])
    kick_ai(room["code"])


def finish_hand_and_next(room: dict, winners: List[dict]) -> None:
    if room["phase"] == "finished":
        push_state(room["code"])
        return

    if len(winners) == 1:
        winners[0]["stack"] += room["pot"]
        log(room, f"🏆 승자: {winners[0]['name']} (+{room['pot']:,}원)")
    else:
        share = room["pot"] // len(winners)
        names = ", ".join(w["name"] for w in winners)
        for w in winners:
            w["stack"] += share
        log(room, f"🤝 무승부 / 공동승리: {names} (각 {share:,}원)")

    # 게임 종료 체크
    alive = [p for p in room["players"] if not p["out"]]
    if len(alive) < 2:
        room["phase"] = "finished"
        log(room, "게임 종료")
        push_state(room["code"])
        return

    room["round_no"] += 1
    room["phase"] = "lobby"  # 잠깐 лobby로 두고 새 판 시작
    push_state(room["code"])
    socketio.sleep(1.0)
    start_betting_hand(room, replay=False)


def resolve_showdown(room: dict) -> None:
    alive = [p for p in room["players"] if is_active(p)]
    if len(alive) <= 1:
        if alive:
            finish_hand_and_next(room, [alive[0]])
        else:
            room["phase"] = "finished"
            push_state(room["code"])
        return

    for p in alive:
        p["rank"] = best_hand(p["hand"])

    # 재경기 우선
    if any(p["rank"]["replay"] for p in alive):
        log(room, "🔥 49 / 멍사구 → 판돈 유지, 2장 재경기")
        start_betting_hand(room, replay=True)
        return

    best = alive[0]
    winners = [best]
    for p in alive[1:]:
        cmp = compare_rank(p["rank"], best["rank"])
        if cmp == 1:
            best = p
            winners = [p]
        elif cmp == 0:
            winners.append(p)

    room["phase"] = "showdown"
    push_state(room["code"])
    socketio.sleep(1.1)
    finish_hand_and_next(room, winners)


def apply_action(room: dict, player_name: str, action: str, from_ai: bool = False) -> None:
    if room["phase"] != "betting":
        return

    current = current_player(room)
    if not current or current["name"] != player_name:
        return

    if player_name not in room["pending"]:
        return

    if action == "die":
        current["folded"] = True
        current["last_action"] = "다이"
        room["pending"].discard(player_name)
        log(room, f"{player_name} 다이")

    elif action == "call":
        if current["stack"] < room["current_to_call"]:
            current["folded"] = True
            current["last_action"] = "다이"
            room["pending"].discard(player_name)
            log(room, f"{player_name} 칩 부족으로 다이")
        else:
            current["stack"] -= room["current_to_call"]
            room["pot"] += room["current_to_call"]
            current["last_action"] = "콜"
            room["pending"].discard(player_name)
            log(room, f"{player_name} 콜")

    elif action == "half":
        raise_amount = room["pot"] // 2
        total = room["current_to_call"] + raise_amount
        if current["stack"] < total:
            current["folded"] = True
            current["last_action"] = "다이"
            room["pending"].discard(player_name)
            log(room, f"{player_name} 칩 부족으로 다이")
        else:
            current["stack"] -= total
            room["pot"] += total
            current["last_action"] = "하프"
            room["current_to_call"] = raise_amount
            room["half_count"] += 1
            room["pending"] = {p["name"] for p in room["players"] if is_active(p) and p["name"] != player_name}
            log(room, f"{player_name} 하프 (콜 {room['current_to_call']:,} / 판돈 {room['pot']:,}원)")

            if not room["third_dealt"] and room["half_count"] >= 4:
                for p in room["players"]:
                    if is_active(p) and len(p["hand"]) == 2:
                        p["hand"].append(draw(room))
                room["third_dealt"] = True
                log(room, "🔥 3장 지급")

    push_state(room["code"])

    alive = [p for p in room["players"] if is_active(p)]
    if len(alive) <= 1:
        if alive:
            room["phase"] = "showdown"
            push_state(room["code"])
            socketio.sleep(0.8)
            finish_hand_and_next(room, [alive[0]])
        else:
            room["phase"] = "finished"
            log(room, "게임 종료")
            push_state(room["code"])
        return

    if not room["pending"]:
        socketio.start_background_task(resolve_showdown, room)


def kick_ai(room_code: str) -> None:
    room = rooms.get(room_code)
    if not room:
        return
    if room.get("ai_running"):
        return
    if room["mode"] != "ai":
        return
    cur = current_player(room)
    if not cur or cur["human"] or room["phase"] != "betting":
        return
    room["ai_running"] = True
    socketio.start_background_task(ai_loop, room_code)


def ai_loop(room_code: str) -> None:
    try:
        while True:
            socketio.sleep(0.7)
            with lock:
                room = rooms.get(room_code)
                if not room or room["phase"] != "betting":
                    return
                cur = current_player(room)
                if not cur or cur["human"] or cur["name"] not in room["pending"]:
                    return
                action = ai_decision(room, cur)

            with lock:
                room = rooms.get(room_code)
                if not room or room["phase"] != "betting":
                    return
                cur = current_player(room)
                if not cur or cur["human"] or cur["name"] not in room["pending"]:
                    return
                apply_action(room, cur["name"], action, from_ai=True)

                if room["phase"] != "betting":
                    return

                cur2 = current_player(room)
                if not cur2 or cur2["human"]:
                    return
    finally:
        with lock:
            room = rooms.get(room_code)
            if room:
                room["ai_running"] = False
        # next kick if needed
        with lock:
            room = rooms.get(room_code)
            if room and room["phase"] == "betting":
                cur = current_player(room)
                if cur and not cur["human"]:
                    socketio.start_background_task(kick_ai, room_code)


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("create_room")
def on_create_room(data):
    name = (data.get("name") or "").strip() or "플레이어"
    mode = data.get("mode", "local")
    room_code = gen_room_code()

    with lock:
        room = {
            "code": room_code,
            "mode": mode,
            "host_sid": request.sid,
            "host_name": name,
            "players": [],
            "phase": "lobby",
            "round_no": 1,
            "pot": 0,
            "current_to_call": ANTE,
            "half_count": 0,
            "third_dealt": False,
            "deck": [],
            "pending": set(),
            "turn_index": None,
            "started": False,
            "log": [],
            "ai_running": False,
        }
        rooms[room_code] = room
        sid_to_room[request.sid] = room_code

        human_name = unique_name(room, name)
        room["players"].append({
            "sid": request.sid,
            "name": human_name,
            "human": True,
            "stack": START_STACK,
            "hand": [],
            "folded": False,
            "out": False,
            "last_action": "",
            "rank": None,
            "chosen": [],
        })

        if mode == "ai":
            for i in range(1, MAX_PLAYERS):
                room["players"].append({
                    "sid": None,
                    "name": f"AI{i}",
                    "human": False,
                    "stack": START_STACK,
                    "hand": [],
                    "folded": False,
                    "out": False,
                    "last_action": "",
                    "rank": None,
                    "chosen": [],
                })
            room["started"] = True
            start_betting_hand(room, replay=False)
        else:
            log(room, f"방 생성됨 ({room_code})")
            push_state(room_code)

    emit("room_created", {"room_code": room_code, "mode": mode, "name": human_name})
    if mode == "ai":
        emit("state", room_public_state(room, request.sid))


@socketio.on("join_room")
def on_join_room(data):
    room_code = (data.get("room_code") or "").strip().upper()
    name = (data.get("name") or "").strip() or "플레이어"
    with lock:
        room = rooms.get(room_code)
        if not room:
            emit("error_msg", {"message": "방을 찾을 수 없음"})
            return
        if room["mode"] != "local":
            emit("error_msg", {"message": "이 방은 AI 모드입니다"})
            return
        if room["phase"] != "lobby":
            emit("error_msg", {"message": "이미 게임이 시작됨"})
            return
        humans = [p for p in room["players"] if p["human"] and not p["out"]]
        if len(humans) >= MAX_PLAYERS:
            emit("error_msg", {"message": "방이 꽉 참"})
            return

        unique = unique_name(room, name)
        room["players"].append({
            "sid": request.sid,
            "name": unique,
            "human": True,
            "stack": START_STACK,
            "hand": [],
            "folded": False,
            "out": False,
            "last_action": "",
            "rank": None,
            "chosen": [],
        })
        sid_to_room[request.sid] = room_code
        log(room, f"{unique} 입장")
        push_state(room_code)

    emit("room_joined", {"room_code": room_code, "name": unique})
    with lock:
        room = rooms.get(room_code)
        if room:
            emit("state", room_public_state(room, request.sid))


@socketio.on("start_game")
def on_start_game():
    room_code = sid_to_room.get(request.sid)
    if not room_code:
        return
    with lock:
        room = rooms.get(room_code)
        if not room:
            return
        if room["host_sid"] != request.sid:
            return
        if room["mode"] != "local":
            return
        if room["phase"] != "lobby":
            return
        if len([p for p in room["players"] if p["human"] and not p["out"]]) < 2:
            emit("error_msg", {"message": "최소 2명 필요"})
            return
        room["started"] = True
        start_betting_hand(room, replay=False)


@socketio.on("action")
def on_action(data):
    action = data.get("action")
    room_code = sid_to_room.get(request.sid)
    if not room_code:
        return

    with lock:
        room = rooms.get(room_code)
        if not room:
            return
        if room["phase"] != "betting":
            return
        player = None
        for p in room["players"]:
            if p["sid"] == request.sid:
                player = p
                break
        if not player:
            return
        apply_action(room, player["name"], action)

    # AI가 다음 턴이면 재가동
    with lock:
        room = rooms.get(room_code)
        if room and room["phase"] == "betting":
            cur = current_player(room)
            if cur and not cur["human"]:
                kick_ai(room_code)


@socketio.on("restart_room")
def on_restart_room():
    room_code = sid_to_room.get(request.sid)
    if not room_code:
        return
    with lock:
        room = rooms.get(room_code)
        if not room:
            return
        if room["host_sid"] != request.sid:
            return
        for p in room["players"]:
            if p["human"]:
                p["stack"] = START_STACK
                p["out"] = False
        for p in room["players"]:
            if not p["human"]:
                p["stack"] = START_STACK
                p["out"] = False
        room["phase"] = "lobby"
        room["round_no"] = 1
        room["pot"] = 0
        room["current_to_call"] = ANTE
        room["half_count"] = 0
        room["third_dealt"] = False
        room["pending"] = set()
        room["turn_index"] = None
        room["log"] = []
        room["started"] = False
        if room["mode"] == "ai":
            start_betting_hand(room, replay=False)
        else:
            push_state(room_code)

    with lock:
        room = rooms.get(room_code)
        if room:
            push_state(room_code)


@socketio.on("disconnect")
def on_disconnect():
    room_code = sid_to_room.pop(request.sid, None)
    if not room_code:
        return
    with lock:
        room = rooms.get(room_code)
        if not room:
            return
        # 소유자/참가자 제거
        for p in room["players"]:
            if p["sid"] == request.sid:
                p["out"] = True
                p["folded"] = True
                p["sid"] = None
                p["last_action"] = "퇴장"
                break
        if room["host_sid"] == request.sid:
            room["host_sid"] = room["players"][0]["sid"] if room["players"] else None
            room["host_name"] = room["players"][0]["name"] if room["players"] else "플레이어"
        log(room, "플레이어 퇴장")
        # AI 게임에서 인간이 나가면 자동 종료 방지용
        push_state(room_code)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
