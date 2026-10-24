"""离线测试：不启动 AstrBot，用 FakeClock / FakeEvent 跑通聚合与统计。

跑法（要用 AstrBot 自带的 Python 才能 import 真实的 astrbot）：

    & "$env:LOCALAPPDATA\\AstrBot\\backend\\python\\python.exe" tests/run_tests.py

设计上刻意**不依赖 pytest**（核心目标之一是零第三方依赖），所以自带一个极简断言器；
`FakeClock` 也是硬要求——真等 12s/60s 的用例没法跑。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

FAILED: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言。

    Args:
        name: 用例名。
        ok: 是否通过。
        detail: 失败时的补充信息。
    """
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [ok]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------- Fake 基础设施
class FakeClock:
    """可控时间源：时间只在 `advance()` 时前进。

    这样"2 秒后结算""12 秒总上限"这类用例可以毫秒级跑完，且能断言
    "**还没到点**不应该结算"——用真实 sleep 是做不到的。
    """

    def __init__(self, start: float = 1000.0) -> None:
        """初始化。

        Args:
            start: 起始时间。
        """
        self.t = start
        self._sleepers: list[tuple[float, asyncio.Future]] = []

    def now(self) -> float:
        """当前时间。

        Returns:
            秒。
        """
        return self.t

    async def sleep(self, seconds: float) -> None:
        """挂起直到时间被推进到点。

        Args:
            seconds: 秒。
        """
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.t + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        """推进时间并唤醒到点的挂起者。

        关键细节：**先让出一轮控制权再计算到期者**。`asyncio.create_task` 创建的协程
        要等到下一个挂起点才真正开始执行，因此刚 `reset()` 出来的计时器此时**还没有**
        在 `_sleepers` 里登记。若先算 due，就会把它整轮跳过（这个坑一开始就踩了，
        表现为"永远不到点"）。

        Args:
            seconds: 推进多少秒。
        """
        await settle()
        self.t += seconds
        due = [(deadline, fut) for deadline, fut in self._sleepers if deadline <= self.t]
        self._sleepers = [
            (deadline, fut) for deadline, fut in self._sleepers if deadline > self.t
        ]
        for _deadline, fut in due:
            if not fut.done():
                fut.set_result(None)
        await settle()


async def settle(times: int = 8) -> None:
    """把事件循环推进若干轮，让 create_task 出来的任务跑到下一个挂起点。

    Args:
        times: 轮数。
    """
    for _ in range(times):
        await asyncio.sleep(0)


class FakeMessageObj:
    """最小消息对象。"""

    def __init__(self, components: list[Any], message_id: str,
                 raw: dict | None = None) -> None:
        """初始化。

        Args:
            components: 组件列表。
            message_id: 消息 ID。
            raw: 原始消息字典。
        """
        self.message = components
        self.message_id = message_id
        self.message_str = "".join(
            getattr(c, "text", "") for c in components
            if hasattr(c, "text") and not getattr(c, "url", None)
        )
        self.raw_message = raw if raw is not None else {}
        self.type = None


class FakeEvent:
    """够用的 AstrMessageEvent 替身。"""

    _seq = 0

    def __init__(self, text: str, *, umo: str = "aiocqhttp:group:1",
                 sender: str = "u1", components: list[Any] | None = None,
                 raw: dict | None = None, message_id: str | None = None,
                 private: bool = False) -> None:
        """初始化。

        Args:
            text: 文本（无组件时用于构造 Plain）。
            umo: 统一消息来源。
            sender: 发送者 ID。
            components: 组件列表（优先于 text）。
            raw: 原始消息。
            message_id: 消息 ID。
            private: 是否私聊。
        """
        FakeEvent._seq += 1
        from astrbot.api.message_components import Plain

        comps = components if components is not None else [Plain(text=text)]
        self.message_str = text
        self.unified_msg_origin = umo
        self._sender = sender
        self._private = private
        self.message_obj = FakeMessageObj(
            comps,
            message_id if message_id is not None else f"m{FakeEvent._seq}",
            raw=raw,
        )
        self.stopped = False

    # --- 事件契约 ---
    def stop_event(self) -> None:
        """停止传播。"""
        self.stopped = True

    def is_stopped(self) -> bool:
        """是否已停止。

        Returns:
            停止标记。
        """
        return self.stopped

    def get_sender_id(self) -> str:
        """发送者 ID。

        Returns:
            ID。
        """
        return self._sender

    def get_self_id(self) -> str:
        """机器人 ID。

        Returns:
            ID。
        """
        return "bot"

    def is_private_chat(self) -> bool:
        """是否私聊。

        Returns:
            True 表示私聊。
        """
        return self._private

    def get_platform_name(self) -> str:
        """平台名。

        Returns:
            平台名。
        """
        return "aiocqhttp"

    def get_platform_id(self) -> str:
        """平台 ID。

        Returns:
            平台 ID。
        """
        return "aiocqhttp"

    def get_messages(self) -> list[Any]:
        """组件列表。

        Returns:
            组件列表。
        """
        return list(self.message_obj.message)

    def plain_result(self, text: str) -> Any:
        """构造纯文本结果。

        Args:
            text: 文本。

        Returns:
            简单对象。
        """
        return SimpleNamespace(text=text)

    def track_temporary_local_file(self, path: str) -> None:
        """记录临时文件（4.26.5 有此 API，无 untrack）。"""


class FakeContext:
    """最小 Context：只提供 register_web_api 与 llm 无关的部分。"""

    def __init__(self) -> None:
        """初始化。"""
        self.web_apis: list[tuple[str, Any, list[str], str]] = []

    def register_web_api(self, route: str, handler: Any, methods: list[str],
                         desc: str) -> None:
        """记录注册。

        Args:
            route: 路由。
            handler: 处理函数。
            methods: 方法列表。
            desc: 描述。
        """
        self.web_apis.append((route, handler, methods, desc))


# ---------------------------------------------------------------- 导入插件包
def install_as_package(tmp_root: Path) -> None:
    """把插件按 AstrBot 的目录约定挂到临时目录，用真实包路径导入。

    Args:
        tmp_root: 临时根目录（充当 data 目录）。
    """
    target = tmp_root / "data" / "plugins" / PLUGIN_DIR.name
    shutil.copytree(
        PLUGIN_DIR,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".git", "dist"),
    )
    sys.path.insert(0, str(tmp_root))


MODULE: Any = None
MAIN: Any = None


def make_plugin(tmp_root: Path, config: dict | None = None,
                clock: Any = None) -> Any:
    """构造插件实例并把数据目录指向临时路径。

    Args:
        tmp_root: 临时目录。
        config: 配置覆盖。
        clock: 时间源。

    Returns:
        插件实例。
    """
    data_dir = tmp_root / "plugin_data" / PLUGIN_DIR.name
    data_dir.mkdir(parents=True, exist_ok=True)
    MAIN.MsgAggregator._data_dir = lambda self: str(data_dir)
    plugin = MAIN.MsgAggregator(FakeContext(), config or {})
    if clock is not None:
        plugin.clock = clock
        plugin.store.clock = clock
        plugin.stats.clock = clock
        plugin.attachments.clock = clock
    return plugin


# ---------------------------------------------------------------- 用例
def test_config_consistency() -> None:
    """配置默认值必须与 `_conf_schema.json` 逐项一致。"""
    print("\n[1] 配置默认值一致性")
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    defaults = MODULE.Config()
    missing = [k for k in schema if not hasattr(defaults, k)]
    check("schema 里的每个键都有对应字段", not missing, str(missing))

    mismatch = []
    for key, spec in schema.items():
        if not hasattr(defaults, key):
            continue
        want = spec.get("default")
        got = getattr(defaults, key)
        if isinstance(want, float) or isinstance(got, float):
            if abs(float(want) - float(got)) > 1e-9:
                mismatch.append((key, want, got))
        elif want != got:
            mismatch.append((key, want, got))
    check("默认值逐项相等（防漂移）", not mismatch, str(mismatch[:5]))

    # 双向校验：Config 里的字段也必须出现在 schema 里，否则用户在 WebUI 看不到它，
    # 而"代码里有、界面没有"的开关最容易被误以为是 bug。
    from dataclasses import fields as dc_fields

    invisible = sorted({f.name for f in dc_fields(MODULE.Config)} - set(schema))
    check("Config 的每个字段都在 schema 里可见（双向一致）", not invisible, str(invisible))


def test_presets_and_validation() -> None:
    """预设覆盖与非法值回退。"""
    print("\n[2] 预设与校验")
    fast = MODULE.load_config({"preset": "fast"})
    check("fast 预设压到 1.0s 且关自适应", fast.debounce_time == 1.0 and not fast.adaptive,
          f"{fast.debounce_time}/{fast.adaptive}")

    patient = MODULE.load_config({"preset": "patient"})
    check("patient 预设 3.5s 且总上限 20s",
          patient.debounce_time == 3.5 and patient.adaptive_max_total_wait == 20.0)

    unknown = MODULE.load_config({"preset": "nope"})
    check("未知预设回退 balanced", unknown.preset == "balanced" and unknown.debounce_time == 2.0)

    bad = MODULE.load_config({"debounce_time": 0})
    check("debounce_time=0 回退 2.0（不是静默禁用整个插件）", bad.debounce_time == 2.0)

    order = MODULE.load_config({"adaptive_min_wait": 9, "adaptive_max_wait": 3,
                                "adaptive_max_total_wait": 1})
    check("阈值顺序非法时整体回退",
          order.adaptive_min_wait <= order.adaptive_max_wait <= order.adaptive_max_total_wait)

    clamp = MODULE.load_config({"stats_flush_interval": 0, "page_push_interval": 100,
                                "stats_history_size": 99999})
    check("统计相关项被 clamp 到安全范围",
          clamp.stats_flush_interval >= 5 and clamp.page_push_interval <= 10
          and clamp.stats_history_size <= 1000)

    alias = MODULE.load_config({"wait_time": 1.5})
    check("旧扁平键 wait_time 映射到 debounce_time", alias.debounce_time == 1.5)


def test_strategy() -> None:
    """等待策略（纯函数）。"""
    print("\n[3] 等待策略")
    cfg = MODULE.Config()
    fixed = MODULE.strategy.compute(
        now=100.0, started_at=100.0, text_len=5, tail_char="", short_streak=1,
        cooldown_remaining=0.0, cfg=MODULE.Config(adaptive=False, debounce_time=2.0),
    )
    check("非自适应 = debounce_time", fixed == 2.0, str(fixed))

    cfg = MODULE.Config()
    adaptive = MODULE.strategy.compute(
        now=100.0, started_at=100.0, text_len=80, tail_char="", short_streak=0,
        cooldown_remaining=0.0, cfg=cfg,
    )
    check("长文本自适应延长且不超过上限",
          cfg.adaptive_min_wait <= adaptive <= cfg.adaptive_max_wait, str(adaptive))

    terminal = MODULE.strategy.compute(
        now=100.0, started_at=100.0, text_len=80, tail_char="。", short_streak=0,
        cooldown_remaining=0.0, cfg=cfg,
    )
    check("结尾是句号时比未结尾短（句子已说完）", terminal < adaptive, f"{terminal} < {adaptive}")

    capped = MODULE.strategy.compute(
        now=111.0, started_at=100.0, text_len=80, tail_char="", short_streak=0,
        cooldown_remaining=0.0, cfg=cfg,
    )
    check("总上限生效：只剩 1 秒", abs(capped - 1.0) < 1e-6, str(capped))

    negative = MODULE.strategy.compute(
        now=120.0, started_at=100.0, text_len=80, tail_char="", short_streak=0,
        cooldown_remaining=0.0, cfg=cfg,
    )
    check("超过总上限时返回 <=0（调用方应立即结算）", negative <= 0, str(negative))

    cooled = MODULE.strategy.compute(
        now=100.0, started_at=100.0, text_len=5, tail_char="", short_streak=0,
        cooldown_remaining=0.8,
        cfg=MODULE.Config(adaptive=False, debounce_time=0.2, min_interval_group=0.8),
    )
    check("cooldown 作为保护网抬到 0.8", abs(cooled - 0.8) < 1e-6, str(cooled))


async def test_timer() -> None:
    """计时器：复用、不自取消、暂停恢复。"""
    print("\n[4] 计时器")
    clock = FakeClock()
    fired: list[int] = []
    timer = MODULE.DebounceTimer(clock, on_fire=lambda: fired.append(1))

    timer.reset(2.0)
    await clock.advance(1.0)
    check("未到点不触发", fired == [], str(fired))
    timer.reset(2.0)
    await clock.advance(1.5)
    check("reset 后从新起点计时（旧任务已被取消）", fired == [], str(fired))
    await clock.advance(0.6)
    check("到期触发一次", len(fired) == 1, str(fired))

    # 不自取消：回调内部若再次 reset（模拟 _on_fire 里取消计时器），当前任务不应受影响
    inner: list[str] = []
    holder: dict[str, Any] = {}

    def on_fire() -> None:
        inner.append("fired")
        holder["timer"].cancel()  # 模拟 begin_finalize 里的 timer.cancel()

    holder["timer"] = MODULE.DebounceTimer(clock, on_fire=on_fire)
    holder["timer"].reset(1.0)
    await clock.advance(1.0)
    check("回调内 cancel 不炸（不自取消，I19）", inner == ["fired"], str(inner))

    # 暂停 / 恢复
    hits: list[int] = []
    t2 = MODULE.DebounceTimer(clock, on_fire=lambda: hits.append(1))
    t2.reset(3.0)
    await clock.advance(1.0)
    t2.pause()
    await clock.advance(10.0)
    check("暂停期间不触发", hits == [], str(hits))
    check("暂停剩余约 2 秒", abs(t2.remaining() - 2.0) < 0.05, str(t2.remaining()))
    t2.resume()
    await clock.advance(1.9)
    check("恢复后仍不触发", hits == [], str(hits))
    await clock.advance(0.2)
    check("恢复后到期触发", len(hits) == 1, str(hits))


async def test_store() -> None:
    """SessionStore：身份校验、幂等 abort、索引、配额。"""
    print("\n[5] 状态存储")
    clock = FakeClock()
    ended: list[int] = []
    cfg = MODULE.Config()
    store = MODULE.SessionStore(clock=clock, cfg=cfg,
                                on_round_end=lambda r: ended.append(r.round_id))

    event = FakeEvent("hi", umo="g:1", sender="u1")
    key = (event.unified_msg_origin, "u1")
    rnd = store.get_or_create(key, event, channel="group")
    check("同键复用同一轮次（I1）", store.get_or_create(key, event) is rnd)

    from astrbot.api.message_components import Plain
    from data.plugins.astrbot_plugin_msg_aggregator import event_io  # type: ignore

    parsed = event_io.parse_incoming(FakeEvent("hello"), cfg)
    item = parsed.to_item(clock.now())
    check("append 成功", store.append(rnd, item))
    check("message_id 建了索引（I9）", store.index.get(item.message_id) == key)

    check("首次退役成功", store.begin_finalize(rnd) is True)
    check("索引已被清除", item.message_id not in store.index)
    check("第二轮退役返回 False（身份不符）", store.begin_finalize(rnd) is False)
    check("退役后进入 finalizing（按 round_id）", store.finalizing.get(rnd.round_id) is rnd)

    store.end_finalize(rnd, cooldown=0.5)
    check("结束回调触发一次", ended.count(rnd.round_id) == 1, str(ended))
    store.end_finalize(rnd, cooldown=0.5)
    check("重复收尾不重复回调（幂等）", ended.count(rnd.round_id) == 1, str(ended))
    check("cooldown 已写入", store.cooldown_remaining(key) > 0)

    # abort 三态幂等
    rnd2 = store.get_or_create(("g:2", "u2"), FakeEvent("x", umo="g:2"), channel="group")
    store.abort(rnd2, cause="test")
    store.abort(rnd2, cause="test")
    check("active 状态 abort 幂等且只回调一次",
          sum(1 for _ in [rnd2]) == 1 and rnd2.state is MODULE.RoundState.DONE)
    check("abort 也触发结束回调", rnd2.round_id in ended, str(ended))
    check("abort 后 flush/done 都已置位", rnd2.flush.is_set() and rnd2.done.is_set())

    # 轮次配额：超限时只丢图片、保留文本
    store.cfg = MODULE.Config(keep_round_max_bytes=10)
    rnd3 = store.get_or_create(("g:3", "u3"), FakeEvent("y", umo="g:3"), channel="group")
    from data.plugins.astrbot_plugin_msg_aggregator.session import Item, Part  # type: ignore

    big = Item(message_id="m-big", parts=(Part(kind="text", value="保留我"),
                                          Part(kind="image", value="/tmp/x.png")),
               arrived_at=clock.now(), image_bytes=999)
    check("超配额时仍接受文本", store.append(rnd3, big) is True)
    check("超配额时图片被裁掉", all(p.kind != "image" for p in rnd3.items[-1].parts))
    check("文本保留", rnd3.items[-1].text() == "保留我")

    only_image = Item(message_id="m-img", parts=(Part(kind="image", value="/tmp/y.png"),),
                      arrived_at=clock.now(), image_bytes=999)
    check("超配额且无文本时整条丢弃", store.append(rnd3, only_image) is False)


async def test_stats(tmp_root: Path) -> None:
    """统计：一次性归类、有界维度、隐私、落盘降级与恢复。"""
    print("\n[6] 统计")
    cfg = MODULE.Config(stats_max_dimensions=16, stats_history_size=5)
    clock = FakeClock()
    data_dir = tmp_root / "stats-a"
    data_dir.mkdir(parents=True, exist_ok=True)
    stats = MODULE.stats_mod.Stats(data_dir=data_dir, cfg=cfg, clock=clock)

    # 正常一轮
    rnd = SimpleNamespace(round_id=1, key=("g:1", "u1"), channel="group",
                          items=[SimpleNamespace(images=lambda: ("/a.png",))],
                          committed_bytes=100, elapsed_ms=30.0,
                          shutting_down=False, stats_recorded=False)
    stats.add_end(rnd)
    check("flush_total +1", stats.flush_total == 1)
    check("items/images 累计正确", stats.items_total == 1 and stats.images_total == 1)
    check("合并率 = items/flush", abs(stats.merge_ratio() - 1.0) < 1e-9)

    # 被 abort 的轮次只能计一边（本版修的核心缺陷）
    aborted = SimpleNamespace(round_id=2, key=("g:1", "u1"), channel="group",
                              items=[], committed_bytes=0, elapsed_ms=5.0,
                              shutting_down=True, stats_recorded=False)
    stats.add_end(aborted)
    check("abort 只计入 abort_total", stats.abort_total == 1 and stats.flush_total == 1)
    stats.add_end(aborted)
    check("同一轮重复 add_end 不重复计数",
          stats.abort_total == 1 and stats.flush_total == 1)

    # 维度上限
    for idx in range(40):
        r = SimpleNamespace(round_id=100 + idx, key=(f"g:{idx}", f"u{idx}"), channel="group",
                            items=[], committed_bytes=0, elapsed_ms=1.0,
                            shutting_down=False, stats_recorded=False)
        stats.add_end(r)
    check("by_umo 不超上限", len(stats.by_umo) <= 16, str(len(stats.by_umo)))
    check("by_sender 不超上限", len(stats.by_sender) <= 16, str(len(stats.by_sender)))
    check("溢出被计数", stats.dim_overflow_total > 0, str(stats.dim_overflow_total))

    # 隐私红线：历史里不能出现消息正文
    #
    # 注意哨兵必须放在**消息正文**里。把哨兵塞进 umo 是自相矛盾的——umo 属于元数据，
    # 本来就该（截断后）保存，那样测出来的"泄漏"是假阳性。
    sentinel = "SENTINEL-7f3a-正文不许落盘"
    text_item = SimpleNamespace(
        images=lambda: (),
        text=lambda: sentinel,
        parts=(SimpleNamespace(kind="text", value=sentinel),),
    )
    r = SimpleNamespace(round_id=9, key=("g:privacy", "u"), channel="group",
                        items=[text_item], committed_bytes=0,
                        elapsed_ms=2.0, shutting_down=False, stats_recorded=False)
    stats.add_end(r)
    dumped = json.dumps(stats.to_dict(), ensure_ascii=False)
    check("落盘数据不含消息正文", sentinel not in dumped and "正文" not in dumped)

    record_fields = set(MODULE.FlushRecord.__dataclass_fields__)
    expect_fields = {"ts", "round_id", "umo", "sender", "channel", "items",
                     "images", "elapsed_ms", "outcome"}
    check("历史记录字段恰好是文档规定的那 9 个（多一个都可能装正文）",
          record_fields == expect_fields, str(record_fields ^ expect_fields))

    check("落盘成功", stats.save() is True)
    raw = json.loads((data_dir / "stats.json").read_text(encoding="utf-8"))
    check("文件里有 version 字段", raw.get("version") == 1)

    # 恢复
    stats2 = MODULE.stats_mod.Stats(data_dir=data_dir, cfg=cfg, clock=clock)
    stats2.load()
    check("重启后计数恢复", stats2.flush_total == stats.flush_total,
          f"{stats2.flush_total} vs {stats.flush_total}")
    check("重启后合并率恢复", abs(stats2.merge_ratio() - stats.merge_ratio()) < 1e-9)

    # 落盘失败降级：把目录换成一个不可写路径
    bad_dir = tmp_root / "stats-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    stats3 = MODULE.stats_mod.Stats(data_dir=bad_dir, cfg=cfg, clock=clock)
    stats3.path = bad_dir / "nope" / "stats.json"
    (bad_dir / "nope").write_text("i am a file", encoding="utf-8")  # 让 mkdir 失败
    check("落盘失败返回 False 而不是抛异常", stats3.save() is False)

    # 版本不符则丢弃重建
    (data_dir / "stats.json").write_text('{"version": 999, "counters": {}}', encoding="utf-8")
    stats4 = MODULE.stats_mod.Stats(data_dir=data_dir, cfg=cfg, clock=clock)
    stats4.load()
    check("版本不符时重建（计数归零）", stats4.flush_total == 0)


async def test_web_api(tmp_root: Path) -> None:
    """页面 API：校验口径、只读契约、SSE 节流。"""
    print("\n[7] 页面 API")
    clock = FakeClock()
    plugin = make_plugin(tmp_root, {"page_enabled": True, "page_push_interval": 1.0},
                         clock=clock)
    ctx = plugin.context
    MODULE.web_api.register(ctx, plugin)
    routes = [r[0] for r in ctx.web_apis]
    check("注册了 6 个以上路由", len(ctx.web_apis) >= 6, str(len(ctx.web_apis)))
    check("路由以插件名开头",
          all(r.startswith("/astrbot_plugin_msg_aggregator/") for r in routes), str(routes[:2]))

    # limit 口径
    check("limit 非法整数抛 ValueError", _raises(lambda: MODULE.web_api.parse_limit("abc")))
    check("limit 越界被 clamp", MODULE.web_api.parse_limit("99999") == 200)
    check("limit 下界被 clamp", MODULE.web_api.parse_limit("0") == 1)
    check("limit 缺省走默认", MODULE.web_api.parse_limit(None) == 50)

    check("preset 白名单通过", MODULE.web_api.validate_preset("fast") == "fast")
    check("未知 preset 抛 ValueError",
          _raises(lambda: MODULE.web_api.validate_preset("turbo")))
    check("reset 需要确认串", MODULE.web_api.reset_allowed({"confirm": "reset"}))
    check("reset 缺确认串被拒", not MODULE.web_api.reset_allowed({}))
    check("reset 确认串错误被拒", not MODULE.web_api.reset_allowed({"confirm": "RESET"}))

    # page_enabled=false 不注册
    off_plugin = make_plugin(tmp_root / "off", {"page_enabled": False}, clock=clock)
    off_ctx = off_plugin.context
    registered = MODULE.web_api.register(off_ctx, off_plugin)
    check("page_enabled=False 时不注册路由", not registered and not off_ctx.web_apis)

    # 只读契约：handler 不修改 store 结构
    before = (len(plugin.store.active), len(plugin.store.finalizing))
    # 首条消息是 Owner，会挂在 flush 上 —— 必须用 task，不能直接 await
    owner = asyncio.create_task(plugin.handle(FakeEvent("hi", umo="g:web", sender="u1")))
    await settle()
    after = (len(plugin.store.active), len(plugin.store.finalizing))
    check("处理一条消息会创建 1 个 active 轮次", after[0] == before[0] + 1)
    rows = MODULE.observability.snapshot_state(plugin.store)
    check("snapshot_state 是同步函数（不是协程）",
          not asyncio.iscoroutinefunction(MODULE.observability.snapshot_state))
    check("snapshot_state 返回行", isinstance(rows, list) and len(rows) == 1)

    # SSE 首帧
    resp = await MODULE.web_api.PageApi(plugin).api_events()
    check("SSE 返回响应对象", resp is not None)

    await clock.advance(3.0)
    await settle()
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)


def _raises(fn: Any) -> bool:
    """断言调用抛异常。

    Args:
        fn: 无参可调用对象。

    Returns:
        True 表示确实抛了异常。
    """
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


async def test_aggregate_flow(tmp_root: Path) -> None:
    """端到端：三条消息合成一次处理，非 Owner 静默，Owner 被重构。"""
    print("\n[8] 聚合主流程")
    clock = FakeClock()
    plugin = make_plugin(tmp_root / "flow", {}, clock=clock)
    plugin.cfg = MODULE.load_config({"debounce_time": 2.0, "adaptive": False})
    plugin.store.cfg = plugin.cfg

    umo = "aiocqhttp:group:flow"
    e1 = FakeEvent("第一句", umo=umo, sender="u1")
    e2 = FakeEvent("第二句", umo=umo, sender="u1")
    e3 = FakeEvent("第三句", umo=umo, sender="u1")

    owner_task = asyncio.create_task(plugin.handle(e1))
    await settle()
    await plugin.handle(e2)
    await clock.advance(1.5)
    await plugin.handle(e3)
    await settle()

    check("非 Owner 事件被静默", e2.stopped and e3.stopped, f"{e2.stopped}/{e3.stopped}")
    check("Owner 尚未被静默（要放行给下游）", not e1.stopped)

    await clock.advance(1.9)
    check("尚未到期不结算", not owner_task.done(), f"done={owner_task.done()}")

    await clock.advance(0.2)
    await settle()
    check("到期后 Owner 返回", owner_task.done())
    check("三条合并成一次处理", plugin.stats.flush_total == 1, str(plugin.stats.flush_total))
    check("合并文本按分隔符拼接",
          e1.message_str == "第一句\n第二句\n第三句", repr(e1.message_str))
    check("item 计数正确", plugin.stats.items_total == 3, str(plugin.stats.items_total))
    check("合并率 3.0", abs(plugin.stats.merge_ratio() - 3.0) < 1e-9)
    check("历史里只留一条", len(plugin.stats.history) == 1)

    # 不同发送者不合并
    #
    # 这两条各自是新桶的**第一条**（Owner），`await plugin.handle(...)` 会永远挂在
    # flush 上——所以必须用 task，事后取消。这是本测试套件最容易踩的坑。
    ta = asyncio.create_task(plugin.handle(FakeEvent("a", umo=umo, sender="ua")))
    tb = asyncio.create_task(plugin.handle(FakeEvent("b", umo=umo, sender="ub")))
    await settle()
    check("同群两人各自成桶（I8）", len(plugin.store.active) == 2,
          str(len(plugin.store.active)))
    ta.cancel()
    tb.cancel()
    await asyncio.gather(ta, tb, return_exceptions=True)


async def test_group_isolation_and_command(tmp_root: Path) -> None:
    """群聊隔离、私聊退化、指令打断。"""
    print("\n[9] 群聊隔离与指令")
    clock = FakeClock()
    plugin = make_plugin(tmp_root / "iso",
                         {"debounce_time": 2.0, "adaptive": False, "interrupt_wait": 0.2},
                         clock=clock)
    plugin.cfg = MODULE.load_config({"debounce_time": 2.0, "adaptive": False,
                                     "interrupt_wait": 0.2})
    plugin.store.cfg = plugin.cfg

    # group_enabled=False → 群聊直接放行（不静默、不缓冲）
    plugin.cfg = MODULE.load_config({"group_enabled": False})
    plugin.store.cfg = plugin.cfg
    g = FakeEvent("群里的话", umo="aiocqhttp:group:x", sender="u1")
    await plugin.handle(g)
    check("关掉群聊合并后不建轮次", not plugin.store.active)
    check("关掉群聊合并也不静默", not g.stopped)

    plugin.cfg = MODULE.load_config({"debounce_time": 2.0, "adaptive": False})
    plugin.store.cfg = plugin.cfg

    # 私聊退化：sender 恒定 → 等价按会话
    p1 = FakeEvent("私聊1", umo="aiocqhttp:private:z", sender="same", private=True)
    task = asyncio.create_task(plugin.handle(p1))
    await settle()
    p2 = FakeEvent("私聊2", umo="aiocqhttp:private:z", sender="same", private=True)
    await plugin.handle(p2)
    check("私聊两条进同一桶", len(plugin.store.active) == 1)
    check("私聊里第二条被静默", p2.stopped)

    # 指令打断：先结算，再放行指令（不静默）
    cmd = FakeEvent("/help", umo="aiocqhttp:private:z", sender="same", private=True)
    await plugin.handle(cmd)
    check("指令本身不被静默（要放行给下游）", not cmd.stopped)
    check("指令触发了一次中断计数", plugin.stats.interrupt_total >= 1,
          str(plugin.stats.interrupt_total))
    await clock.advance(3.0)
    await settle()

    # 空内容
    empty = FakeEvent("   ", umo="aiocqhttp:group:y", sender="u9")
    await plugin.handle(empty)
    check("空消息不建轮次", not plugin.store.active)


async def test_typing_and_recall(tmp_root: Path) -> None:
    """输入状态与撤回。"""
    print("\n[10] 输入状态与撤回")
    clock = FakeClock()
    plugin = make_plugin(tmp_root / "typing",
                         {"debounce_time": 2.0, "adaptive": False, "max_typing_wait": 60},
                         clock=clock)
    plugin.cfg = MODULE.load_config({"debounce_time": 2.0, "adaptive": False,
                                     "max_typing_wait": 60})
    plugin.store.cfg = plugin.cfg

    umo = "aiocqhttp:group:t"
    owner_task = asyncio.create_task(plugin.handle(FakeEvent("开始", umo=umo, sender="u1")))
    await settle()
    rnd = list(plugin.store.active.values())[0]

    typing = FakeEvent("", umo=umo, sender="u1",
                       raw={"post_type": "notice", "notice_type": "input_status",
                            "event_type": 1, "status_text": "正在输入"})
    await plugin.handle(typing)
    check("进入 TYPING_PAUSED", rnd.state is MODULE.RoundState.TYPING_PAUSED, rnd.state.value)
    check("计时器被暂停", rnd.typing)

    await clock.advance(5.0)
    check("暂停期间不结算", not owner_task.done())

    stop_typing = FakeEvent("", umo=umo, sender="u1",
                            raw={"post_type": "notice", "notice_type": "input_status",
                                 "event_type": 2, "status_text": "停止输入"})
    await plugin.handle(stop_typing)
    check("恢复为 COLLECTING", rnd.state is MODULE.RoundState.COLLECTING, rnd.state.value)
    await clock.advance(2.1)
    await settle()
    check("恢复后到期结算", owner_task.done())

    # typing 判定失败 → 三态之 None
    unknown = MODULE.caps_mod.probe_typing({"notice_type": "input_status"})
    check("缺少结构化字段与文案时返回 None", unknown is None)

    # 撤回：按索引定位并标记
    clock2 = FakeClock()
    plugin.cfg = MODULE.load_config({"debounce_time": 2.0, "adaptive": False})
    plugin.store.cfg = plugin.cfg
    ev_a = FakeEvent("会被撤回", umo=umo, sender="u2", message_id="mid-1")
    task_a = asyncio.create_task(plugin.handle(ev_a))
    await settle()
    recall = FakeEvent("", umo=umo, sender="u2",
                       raw={"post_type": "notice", "notice_type": "group_recall",
                            "message_id": "mid-1"})
    await plugin.handle(recall)
    rnd_a = list(plugin.store.active.values())[0]
    check("撤回后条目被标记", any(item.recalled for item in rnd_a.items),
          str([i.recalled for i in rnd_a.items]))
    await clock.advance(2.5)
    await settle()
    check("整轮都被撤回时按 empty 丢弃",
          task_a.done() and plugin.stats.drop_by_cause.get("empty", 0) >= 1,
          str(plugin.stats.drop_by_cause))


async def test_shutdown(tmp_root: Path) -> None:
    """关停：静默进行中的轮次、不写 cooldown、统计落盘。"""
    print("\n[11] 关停")
    clock = FakeClock()
    plugin = make_plugin(tmp_root / "shutdown", {"debounce_time": 5.0}, clock=clock)
    plugin.cfg = MODULE.load_config({"debounce_time": 5.0})
    plugin.store.cfg = plugin.cfg

    umo = "aiocqhttp:group:sd"
    owner = FakeEvent("挂着的话", umo=umo, sender="u1")
    task = asyncio.create_task(plugin.handle(owner))
    await settle()
    check("有一个挂起的轮次", len(plugin.store.active) == 1)

    await plugin.shutdown()
    await settle()
    check("Owner 被静默（不产生多余回复）", owner.stopped)
    check("轮次被 abort 记入统计", plugin.stats.abort_total >= 1, str(plugin.stats.abort_total))
    check("shutdown 不写 cooldown", not plugin.store.cooldown)
    check("shutdown 后 active 为空", not plugin.store.active)
    check("统计文件已写出", (tmp_root / "shutdown" / "plugin_data" /
                            PLUGIN_DIR.name / "stats.json").is_file())
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_images(tmp_root: Path) -> None:
    """图片跨事件保活（复制到缓存目录）。"""
    print("\n[12] 附件")
    from astrbot.api.message_components import Image, Plain

    clock = FakeClock()
    plugin = make_plugin(tmp_root / "img", {"debounce_time": 2.0, "adaptive": False},
                         clock=clock)
    plugin.cfg = MODULE.load_config({"debounce_time": 2.0, "adaptive": False})
    plugin.store.cfg = plugin.cfg

    src = tmp_root / "src.png"
    src.write_bytes(b"\x89PNG" + b"0" * 200)

    umo = "aiocqhttp:group:img"
    e1 = FakeEvent("看图", umo=umo, sender="u1")
    task = asyncio.create_task(plugin.handle(e1))
    await settle()
    e2 = FakeEvent("", umo=umo, sender="u1",
                   components=[Image(file=str(src))])
    await plugin.handle(e2)
    rnd = list(plugin.store.active.values())[0]
    check("第二条消息追加进同一轮", len(rnd.items) == 2, str(len(rnd.items)))

    images = [p.value for item in rnd.items for p in item.parts if p.kind == "image"]
    check("图片被复制到缓存目录", images and str(plugin.attachments.cache_dir) in images[0],
          str(images))
    from pathlib import Path as _P
    check("缓存文件真实存在", images and _P(images[0]).is_file())

    await clock.advance(2.5)
    await settle()
    check("结算后图片仍在组件链里",
          any(isinstance(c, Image) for c in e1.message_obj.message),
          str([type(c).__name__ for c in e1.message_obj.message]))


# ---------------------------------------------------------------- 入口
async def test_preemption(tmp_root: Path) -> None:
    """抢占检测：本该处理却没处理到 → 可见告警，而不是静默不工作。"""
    print("\n[14] 抢占检测（优先级不足的根源修复）")
    clock = FakeClock()
    plugin = make_plugin(tmp_root / "preempt", {}, clock=clock)

    # 正常情况：被处理过的消息绝不误报
    seen = FakeEvent("正常消息", umo="aiocqhttp:group:p", sender="u1")
    task = asyncio.create_task(plugin.handle(seen))
    await settle()
    plugin.check_preempted(seen)
    check("已处理过的消息不误报", plugin.stats.preempted_total == 0,
          str(plugin.stats.preempted_total))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # 被抢占：事件从未进入 handle，但走到了 LLM 请求
    victim = FakeEvent("本该聚合的话", umo="aiocqhttp:group:p", sender="u2")
    check("未处理的消息未被标记", not getattr(victim, MAIN.MARKER_ATTR, False))
    plugin.check_preempted(victim)
    check("被抢占时计数 +1", plugin.stats.preempted_total == 1,
          str(plugin.stats.preempted_total))
    plugin.check_preempted(victim)
    check("同一事件重复检测不重复计数（幂等）", plugin.stats.preempted_total == 1,
          str(plugin.stats.preempted_total))

    # 不该处理的消息不能误报
    before = plugin.stats.preempted_total
    plugin.check_preempted(FakeEvent("", umo="aiocqhttp:group:p", sender="u3"))
    check("空消息不误报", plugin.stats.preempted_total == before)
    plugin.check_preempted(FakeEvent("/help", umo="aiocqhttp:group:p", sender="u3"))
    check("指令不误报", plugin.stats.preempted_total == before)
    plugin.check_preempted(
        FakeEvent("", umo="aiocqhttp:group:p", sender="u3",
                  raw={"post_type": "notice", "notice_type": "input_status",
                       "event_type": 1})
    )
    check("通知事件不误报", plugin.stats.preempted_total == before)

    # 关掉群聊合并后，群消息本就不该处理 → 不误报
    plugin.cfg = MODULE.load_config({"group_enabled": False})
    plugin.store.cfg = plugin.cfg
    plugin.check_preempted(FakeEvent("群消息", umo="aiocqhttp:group:p", sender="u4"))
    check("群聊关闭时不误报", plugin.stats.preempted_total == before)

    # 计数进入统计投影与落盘
    snap = plugin.stats.snapshot(history_limit=0)
    check("preempted_total 进入只读投影", "preempted_total" in snap.counters)
    check("preempted_total 进入落盘数据", "preempted_total" in plugin.stats.to_dict()["counters"])

    # 优先级常量必须处在"插件层顶格"而不是随手一个值
    check("优先级取插件层顶格（>=1000）", MAIN.MESSAGE_PRIORITY >= 1000,
          str(MAIN.MESSAGE_PRIORITY))

    # 钩子真的注册进了 OnLLMRequestEvent（不在派发循环里，优先级挡不住）
    try:
        from astrbot.core.star.star_handler import EventType, star_handlers_registry

        hooks = [
            h.handler_name for h in
            star_handlers_registry.get_handlers_by_event_type(
                EventType.OnLLMRequestEvent, only_activated=False)
            if "msg_aggregator" in h.handler_module_path
        ]
        check("注册了 LLM 请求钩子（抢占检测的落点）",
              "aggregate_on_llm_request" in hooks, str(hooks))
    except Exception as exc:  # noqa: BLE001 - 环境差异时跳过
        print(f"  [skip] 无法查询注册表：{exc}")


def test_metadata() -> None:
    """`metadata.yaml` 必须能被 AstrBot 解析，且关键字段齐全。"""
    print("\n[13] 元信息")
    try:
        import yaml  # AstrBot 自带 PyYAML
    except Exception:  # noqa: BLE001 - 没有 yaml 就跳过（不影响核心用例）
        print("  [skip] 环境没有 PyYAML，跳过元信息校验")
        return

    raw = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    try:
        meta = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001
        check("metadata.yaml 可解析", False, str(exc))
        return
    check("metadata.yaml 可解析", isinstance(meta, dict))

    for key in ("name", "display_name", "short_desc", "version", "astrbot_version"):
        check(f"含必需字段 {key}", bool(meta.get(key)), repr(meta.get(key)))

    check("name 与目录名一致", meta.get("name") == PLUGIN_DIR.name,
          f"{meta.get('name')} vs {PLUGIN_DIR.name}")
    check("version 带 v 前缀", str(meta.get("version", "")).startswith("v"),
          str(meta.get("version")))

    import re

    declared = str(meta.get("astrbot_version", ""))
    check("astrbot_version 形如 >=4.26", bool(re.search(r">=\s*4\.\d+", declared)), declared)


async def main() -> int:
    """跑全部用例。

    Returns:
        退出码。
    """
    global MODULE, MAIN
    tmp_root = Path(tempfile.mkdtemp(prefix="msg-agg-test-"))
    try:
        install_as_package(tmp_root)
        pkg = f"data.plugins.{PLUGIN_DIR.name}"
        MODULE = importlib.import_module(pkg)
        MAIN = importlib.import_module(f"{pkg}.main")

        # 插件包的 __init__.py 是空的（AstrBot 也不需要它导出符号），
        # 而下面的用例习惯写 MODULE.xxx —— 这里显式挂一批别名，保持用例可读。
        for alias, sub, names in (
            ("config", "config", ("Config", "load_config", "describe_effective")),
            ("session", "session", ("SessionStore", "RoundState", "Item", "Part")),
            ("stats", "stats", ("Stats", "Histogram", "FlushRecord")),
            ("timer", "timer", ("DebounceTimer",)),
            ("strategy", "strategy", ("compute",)),
            ("web_api", "web_api", ("register", "PageApi", "parse_limit",
                                    "validate_preset", "reset_allowed")),
            ("caps_mod", "capabilities", ("probe_typing", "is_typing_notice",
                                          "is_recall_notice", "recalled_message_id",
                                          "Capabilities")),
            ("observability", "observability", ("snapshot_state", "log_event")),
            ("event_io", "event_io", ("parse_incoming",)),
        ):
            module = importlib.import_module(f"{pkg}.{sub}")
            setattr(MODULE, alias, module)
            for name in names:
                if hasattr(module, name):
                    setattr(MODULE, name, getattr(module, name))
        if not hasattr(MODULE, "stats_mod"):
            MODULE.stats_mod = getattr(MODULE, "stats")

        test_config_consistency()
        test_presets_and_validation()
        test_strategy()
        await test_timer()
        await test_store()
        await test_stats(tmp_root)
        await test_web_api(tmp_root)
        await test_aggregate_flow(tmp_root)
        await test_group_isolation_and_command(tmp_root)
        await test_typing_and_recall(tmp_root)
        await test_images(tmp_root)
        await test_shutdown(tmp_root)
        await test_preemption(tmp_root)
        test_metadata()
    except Exception:  # noqa: BLE001 - 测试脚本自身出错要显式报出来
        traceback.print_exc()
        FAILED.append("测试脚本自身抛异常")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败用例：")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(asyncio.run(main()))
