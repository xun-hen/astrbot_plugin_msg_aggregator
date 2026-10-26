"""消息聚合插件主流程：拦截 → 缓冲 → 重构。

## 为什么 handler 是**模块级函数**而逻辑在实例方法里

AstrBot 派发 handler 时是 `call_handler(event, handler.handler)` → `handler(event, ...)`。
多数环境注册的是**绑定方法**，`self` 已绑定；但在某些环境（本项目兄弟插件实测过
AstrBot 4.28.1 + webchat）注册进去的是**未绑定的类函数**，此时 `self` 会是事件对象，
任何 `self.xxx` 都会抛 `AttributeError`——而派发层会 `stop_event()`，同一条消息后面
的插件全部不执行。

所以这里采用"模块级函数 + 模块级单例"：入口不依赖 `self`，业务逻辑仍然是实例方法
（便于单测直接构造实例）。两种派发风格都能跑。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import attachments as attachments_mod
from . import capabilities as caps_mod
from . import event_io, stats as stats_mod, strategy, web_api
from .config import PLUGIN_NAME, Config, describe_effective, load_config
from .observability import log_event, snapshot_state
from .session import Round, RoundState, SessionStore
from .timer import DebounceTimer, RealClock

MESSAGE_PRIORITY = 1000
"""拦截型插件的优先级：取**插件层顶格**，而不是"比某个插件高一点"。

为什么不是 50、60 这种值：那些数字是**相对**的。你永远不知道下一个装进来的插件
用的是什么数字，于是"够不够高"变成了一个随环境变化的运气问题。取 1000 的含义是
"在所有正常插件的约定区间（1~100）之上"，从而把"比别人高一点"换成"处在插件层的顶层"。

为什么不取 `sys.maxsize`：框架内置的 `handle_session_control_agent` 用的就是
`sys.maxsize`，与其并列时的先后取决于**注册顺序**（稳定排序），我们控制不了；
而且跟内置抢位本身没有意义——它做的是会话控制，本来就该先跑。

**但优先级再高也只是"尽量先跑"，不是保证。** 真正的根因修复见 `_on_llm_request`
里的抢占检测：不再假设自己一定先跑，而是**被抢占时能发现并告警**，
把失败模式从"静默不工作"降级为"可见的性能下降"。
"""

MARKER_ATTR = "_msg_agg_seen"
"""在事件对象上打的"本插件处理过"标记。

用它而不是外部表：事件对象在同一条消息的整条管线里是同一个实例，
一个属性就能贯穿"消息派发 → LLM 请求"两个阶段，且随事件一起回收、不会泄漏。
"""

SHUTDOWN_GRACE = 2.0
"""shutdown 时等待进行中轮次收尾的秒数。"""

MESSAGE_HANDLER_NAME = "aggregate_on_event"
"""消息入口 handler 的函数名。

AstrBot 用 `handler.__name__` 填 `StarHandlerMetadata.handler_name`，而本插件的入口是
**模块级函数** `aggregate_on_event`（原因见模块 docstring）。抢占检测要拿它去核对
`activated_handlers`：在计划里 = 这条消息本该派发给我们。
"""

_INSTANCE: "MsgAggregator | None" = None


# --------------------------------------------------------------------- handler
@filter.on_llm_request()
async def aggregate_on_llm_request(event: AstrMessageEvent, *_args: Any, **_kwargs: Any) -> None:
    """**抢占检测**（从根源解决"优先级不足"的那一半）。

    这一层不在 `star_request.py` 的派发循环里，因此**任何优先级都挡不住它**。
    它不做聚合（消息已经被别人消费了，没得聚），只回答一个问题：

        这条本该由我处理的消息，我到底处理了吗？

    如果没处理，说明有优先级更高的执行者在派发循环里先 `stop_event()` 了我们——
    这正是"优先级不足"的真实表现。此时我们**不做任何补救动作**（补救只会和对方抢），
    但会：

    1. 记 `aggregate.preempted` 警告（带 UMO 与被谁挤掉的线索）；
    2. 计入 `preempted_total` 统计（页面可见）；
    3. 给用户/维护者一条可执行的建议。

    这才是根因层面的修复：**把"静默不工作"变成"可见的性能下降"**。
    抬优先级只能降低发生概率，检测能让它一旦发生就必然被发现。
    """
    plugin = _INSTANCE
    if plugin is None:
        return
    try:
        plugin.check_preempted(event)
    except Exception as exc:  # noqa: BLE001 - 钩子绝不能影响正常回复
        logger.debug(f"[{PLUGIN_NAME}] 抢占检测异常（已忽略）：{exc}")


@filter.event_message_type(filter.EventMessageType.ALL, priority=MESSAGE_PRIORITY)
async def aggregate_on_event(event: AstrMessageEvent) -> None:
    """唯一入口。

    **必须吞掉所有异常**：handler 抛异常会被派发层 `stop_event()`，
    同一条消息上排在后面的插件就全部不执行了（这个坑兄弟插件踩过）。

    Args:
        event: 消息事件。
    """
    plugin = _INSTANCE
    if plugin is None:
        return
    try:
        await plugin.handle(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 绝不让异常外泄
        logger.exception(f"[{PLUGIN_NAME}] 处理事件时未捕获异常（已兜住）：{exc}")
        try:
            plugin.panic_release(event)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------- 插件主体
class MsgAggregator(Star):
    """消息聚合插件。"""

    def __init__(
        self,
        context: Context,
        config: dict | None = None,
        *,
        message_handler_name: str = MESSAGE_HANDLER_NAME,
        persist_preset: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """初始化。

        Args:
            context: AstrBot 上下文。
            config: 插件配置。生产环境是 `AstrBotConfig`（dict 子类，带
                `save_config()`），测试里可能是普通 dict。
            message_handler_name: 消息入口 handler 的函数名，抢占检测用它核对
                "本次派发计划里有没有我们"。传空串等于关掉抢占检测。
            persist_preset: 切预设后把显式值写回插件配置的回调。缺省用自带的
                `_persist_config`（写回配置对象并 `save_config()`）。
                v0.1.0 只改内存里的 `Config`，重载插件就悄悄回到旧预设。
        """
        super().__init__(context)
        global _INSTANCE
        _INSTANCE = self

        # 原始配置对象要**单独留一份引用**：写回时既要更新值，也要能调 save_config()。
        # 不要 dict(config) 复制——那会丢掉 AstrBotConfig 的方法。
        self._config_obj: dict[str, Any] = config if isinstance(config, dict) else {}
        self.message_handler_name = message_handler_name
        self.persist_preset: Callable[[dict[str, Any]], None] | None = (
            persist_preset or self._persist_config
        )

        self.cfg: Config = load_config(config)
        self.clock = RealClock()
        self._shutting_down = False
        self._started = False
        self._tasks: list[asyncio.Task] = []

        data_dir = Path(self._data_dir())
        data_dir.mkdir(parents=True, exist_ok=True)

        self.stats = stats_mod.Stats(data_dir=data_dir, cfg=self.cfg, clock=self.clock)
        self.stats.load()
        self.stats.track_config(stats_mod.config_hash(self.cfg))

        self.store = SessionStore(
            clock=self.clock, cfg=self.cfg, on_round_end=self._on_round_end
        )
        self.attachments = attachments_mod.Attachments(
            data_dir=data_dir, cfg=self.cfg, clock=self.clock
        )
        self.caps = caps_mod.Capabilities()

        logger.info(
            f"[{PLUGIN_NAME}] 实例已构造 {describe_effective(self.cfg)}"
            f" | 数据目录={data_dir} | 优先级={MESSAGE_PRIORITY}"
        )

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件加载完成：注册页面后端、探测能力、启动后台任务。"""
        self.caps = caps_mod.probe(self)
        web_api.register(self.context, self)
        self._start_tasks()
        logger.info(
            f"[{PLUGIN_NAME}] 已加载 | 能力 {self.caps.as_log_line()}"
            f" | 群聊合并={'开' if self.cfg.group_enabled else '关'}"
            f" | 页面={'开' if self.cfg.page_enabled else '关'}"
        )
        logger.info(
            f"[{PLUGIN_NAME}] 统计已恢复：flush={self.stats.flush_total}"
            f" abort={self.stats.abort_total} 合并率={self.stats.merge_ratio():.2f}"
        )

    async def terminate(self) -> None:
        """插件卸载/重载：静默进行中的轮次，落盘统计，不写 cooldown。"""
        await self.shutdown()

    async def shutdown(self) -> None:
        """优雅关停（I10 + I18：进行中的轮次必须静默，且不写 cooldown）。"""
        if self._shutting_down:
            return
        self._shutting_down = True

        pending = list(self.store.active.values()) + list(self.store.finalizing.values())
        for rnd in pending:
            self.store.abort(rnd, cause="shutdown")
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(r.done.wait() for r in pending)),
                    timeout=SHUTDOWN_GRACE,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[{PLUGIN_NAME}] shutdown 等待 {len(pending)} 个轮次超时"
                    f"（{SHUTDOWN_GRACE}s），已强制继续"
                )

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        self.stats.save()
        log_event("aggregate.flush", umo="-", items=0, images=0,
                  elapsed=0, enrich_ms=0, cause="shutdown")

    def _start_tasks(self) -> None:
        """启动后台任务（统计落盘、缓存清理）。"""
        if self._started:
            return
        self._started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(f"[{PLUGIN_NAME}] 无运行中的事件循环，后台任务延后启动")
            self._started = False
            return
        self._tasks.append(loop.create_task(self._stats_loop()))
        self._tasks.append(loop.create_task(self._housekeeping_loop()))

    async def _stats_loop(self) -> None:
        """按间隔落盘统计（也响应"累计 N 轮"的即时落盘）。"""
        try:
            while not self._shutting_down:
                await asyncio.sleep(max(5.0, float(self.cfg.stats_flush_interval)))
                if self._shutting_down:
                    return
                if self.stats.need_flush() or self.stats.revision != self.stats._last_flush_revision:
                    self.stats.save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 统计落盘任务异常退出：{exc}")

    async def _housekeeping_loop(self) -> None:
        """定期清理过期缓存与 cooldown。"""
        try:
            while not self._shutting_down:
                await asyncio.sleep(3600.0)
                if self._shutting_down:
                    return
                self.attachments.cleanup()
                self.store.gc_cooldown()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 清理任务异常退出：{exc}")

    # ------------------------------------------------------------------ 主流程
    async def handle(self, event: AstrMessageEvent) -> None:
        """分流与编排（文档 §11）。

        Args:
            event: 消息事件。
        """
        if not self.cfg.enabled or self._shutting_down:
            return
        if not self._started:
            self._start_tasks()

        # 打标记：只要进了这里，就说明"我们看到了这条消息"。
        # 抢占检测靠它区分"没被派发到"与"派发到了但没内容可合并"。
        try:
            setattr(event, MARKER_ATTR, True)
        except Exception:  # noqa: BLE001 - 少数事件对象可能有 __slots__
            pass

        # 通知类事件在 `_accepts` **之前**处理：群聊 input_status 是 notice，
        # `is_wake` 未必为真，先做过滤会把群聊输入感知整体误杀（文档 §11.5）。
        if self.cfg.typing_detection and caps_mod.is_typing_notice(event):
            self._on_typing(event)
            return
        if self.cfg.recall_filter and caps_mod.is_recall_notice(event):
            self._on_recall(event)
            return

        if not self._accepts(event):
            return

        parsed = event_io.parse_incoming(event, self.cfg)

        # F4：先判指令，再持久化附件，避免命令消息产生孤儿缓存
        if event_io.is_command(parsed.text(), self.cfg.command_prefixes):
            await self._interrupt(event_io.key_of(event))
            return
        if not parsed.has_content():
            return

        parsed = await self.attachments.persist(parsed, event)

        key = event_io.key_of(event)
        channel = event_io.channel_of(event)
        rnd = self.store.get_or_create(key, event, channel=channel)
        item = parsed.to_item(self.clock.now())
        if not self.store.append(rnd, item):
            return

        log_event("aggregate.append", umo=key[0], items=len(rnd.items),
                  images=len(parsed.images()), wait=round(rnd.timer.remaining(), 2)
                  if rnd.timer else 0)

        # F13：Owner 与非 Owner 都要延长等待（从最后一条起算），
        # 例外是 TYPING_PAUSED（此时不得 reset，否则等于解除暂停，见 §10.1）。
        if rnd.state is not RoundState.TYPING_PAUSED:
            wait = self._compute_wait(rnd)
            self._arm_timer(rnd, wait)

        if rnd.owner_event is not event:
            self._silence(event)
            return

        try:
            await rnd.flush.wait()
            await self._after_flush(rnd)
        except asyncio.CancelledError:
            self.store.abort(rnd, cause="cancelled")
            raise

    def _compute_wait(self, rnd: Round) -> float:
        """算本轮下一次等待。

        Args:
            rnd: 轮次。

        Returns:
            秒数（<= 0 表示立即结算）。
        """
        return strategy.compute(
            now=self.clock.now(),
            started_at=rnd.started_at,
            text_len=rnd.text_len(),
            tail_char=rnd.tail_char(),
            short_streak=rnd.short_streak,
            cooldown_remaining=self.store.cooldown_remaining(rnd.key),
            cfg=self.cfg,
        )

    def _arm_timer(self, rnd: Round, seconds: float) -> None:
        """武装计时器（复用 Round 自带对象，I2）。

        Args:
            rnd: 轮次。
            seconds: 等待秒数。
        """
        if rnd.timer is None:
            rnd.timer = DebounceTimer(self.clock, on_fire=lambda: self._on_fire(rnd))
        wait = max(0.0, float(seconds))
        if rnd.started_at <= 0:
            rnd.started_at = self.clock.now()
        log_event("aggregate.start", umo=rnd.key[0], sender=rnd.key[1],
                  wait=round(wait, 2), reason="adaptive" if self.cfg.adaptive else "fixed")
        rnd.timer.reset(wait)

    def _on_fire(self, rnd: Round) -> None:
        """计时器到期：同步退役 + 放行（I11：同一同步块，中间无 await）。

        Args:
            rnd: 轮次。
        """
        if not self.store.begin_finalize(rnd):
            log_event("aggregate.stale_timer", key=rnd.key, round_id=rnd.round_id)
            return
        rnd.flush.set()

    async def _after_flush(self, rnd: Round) -> None:
        """Owner 从挂起返回后的唯一分支点（I16）。

        注意：abort 之后必须由 Owner **自己**静默，不能裸 return，
        否则该事件会带着首条消息继续流向下游，产生一条多余的回复。

        Args:
            rnd: 轮次。
        """
        if rnd.shutting_down or self._shutting_down:
            self._silence(rnd.owner_event, cause="abandoned")
            self.store.end_finalize(rnd, cooldown=0.0)
            return
        await self._finalize(rnd)

    async def _finalize(self, rnd: Round) -> None:
        """结算（检查点化，I18：每个 `await` 之后都复查放弃）。

        Args:
            rnd: 轮次。
        """
        acquired = False
        started = time.perf_counter()
        try:
            async with self.store.finalize_lock(rnd.key):
                acquired = True
                snap = rnd.frozen or rnd.snapshot()

                # 检查点 ①：入口
                if self._abandoned(rnd):
                    self._silence(rnd.owner_event, cause="abandoned")
                    return

                try:
                    text = snap.text(self.cfg.separator)
                    images = list(snap.images())
                    images = self.attachments.normalize(images)
                    images = _dedupe(images)

                    # 检查点 ②：外部 IO 之后（当前版本没有增强包，保留位置）
                    if self._abandoned(rnd):
                        self._silence(rnd.owner_event, cause="abandoned")
                        return

                    # 检查点 ③：reconstruct 之前（最后一道闸）
                    if self._abandoned(rnd):
                        self._silence(rnd.owner_event, cause="abandoned")
                        return

                    if not text.strip() and not images:
                        self._silence(rnd.owner_event, cause="empty")
                        return

                    if self.cfg.preserve_part_order:
                        event_io.reconstruct_ordered(rnd.owner_event, snap, images, self.cfg)
                    else:
                        event_io.reconstruct(rnd.owner_event, text, images, self.cfg)
                except Exception:
                    logger.exception(f"[{PLUGIN_NAME}] aggregate.finalize_failed")
                    self.store.abort(rnd, cause="finalize_failed")
                    self._silence(rnd.owner_event, cause="failed")
        finally:
            rnd.elapsed_ms = (time.perf_counter() - started) * 1000.0
            # shutdown 期间不写 cooldown（P1-3）
            cooldown = 0.0
            if acquired and not rnd.shutting_down and not self._shutting_down:
                cooldown = self._cooldown_for(rnd)
            self.store.end_finalize(rnd, cooldown)

    def _abandoned(self, rnd: Round) -> bool:
        """放弃判定：单轮 abort 或整插件 shutdown。

        Args:
            rnd: 轮次。

        Returns:
            True 表示应放弃。
        """
        return rnd.shutting_down or self._shutting_down

    def _cooldown_for(self, rnd: Round) -> float:
        """按渠道取 cooldown 秒数（仅当 debounce < min_interval 时才有意义）。

        Args:
            rnd: 轮次。

        Returns:
            秒数。
        """
        interval = (
            self.cfg.min_interval_private if rnd.channel == "private"
            else self.cfg.min_interval_group
        )
        if interval <= 0 or self.cfg.debounce_time >= interval:
            return 0.0
        return float(interval)

    def _on_round_end(self, rnd: Round) -> None:
        """轮次结束回调（交给统计，幂等）。

        Args:
            rnd: 轮次。
        """
        self.stats.add_end(rnd)

    # ------------------------------------------------------------------ 通知事件
    def _on_typing(self, event: AstrMessageEvent) -> None:
        """输入状态通知：只查表、不建表（无副作用）。

        Args:
            event: 通知事件。
        """
        key = event_io.key_of(event)
        rnd = self.store.active.get(key)
        if rnd is None or rnd.timer is None:
            return
        state = caps_mod.probe_typing(caps_mod.raw_of(event))
        if state is None:
            # 判定失败只打 debug：部分适配器每次输入都发 notice，warning 会刷屏
            self.stats.add_typing_unknown()
            log_event("aggregate.typing_unknown", umo=key[0])
            return
        if state:
            if rnd.typing:
                return
            rnd.typing = True
            rnd.state = RoundState.TYPING_PAUSED
            rnd.timer.pause()
            rnd.typing_guard = asyncio.create_task(self._typing_watchdog(rnd))
            log_event("aggregate.pause", umo=key[0], cause="typing",
                      max_wait=self.cfg.max_typing_wait)
        elif rnd.typing:
            rnd.typing = False
            rnd.state = RoundState.COLLECTING
            guard = rnd.typing_guard
            if guard is not None and not guard.done():
                current = asyncio.current_task()
                if guard is not current:
                    guard.cancel()
            rnd.typing_guard = None
            rnd.timer.resume()

    async def _typing_watchdog(self, rnd: Round) -> None:
        """输入状态的最长暂停兜底。

        必须走注入的时钟而不是 `asyncio.sleep`：否则测试里 60 秒就只能真等，
        也就没法断言"超过 max_typing_wait 会自动结算"。

        Args:
            rnd: 轮次。
        """
        try:
            await self.clock.sleep(max(1.0, float(self.cfg.max_typing_wait)))
        except asyncio.CancelledError:
            return
        self._on_fire(rnd)

    def _on_recall(self, event: AstrMessageEvent) -> None:
        """撤回通知：按索引定位桶并标记（只查表，无副作用）。

        Args:
            event: 通知事件。
        """
        mid = caps_mod.recalled_message_id(event)
        if mid is None:
            return
        key = self.store.index.get(mid)
        if key is None:
            return
        rnd = self.store.active.get(key)
        if rnd is None:
            # 已退役：冻结快照不再接受撤回（文档 §11.6）
            return
        from dataclasses import replace as dc_replace

        for idx, item in enumerate(rnd.items):
            if item.message_id == mid:
                rnd.items[idx] = dc_replace(item, recalled=True)
                self.caps.mark_recall_seen()
                log_event("aggregate.recall", umo=key[0], message_id=mid,
                          remaining=len(rnd.items) - 1)
                return

    # ------------------------------------------------------------------ 指令中断
    async def _interrupt(self, key: tuple[str, str]) -> None:
        """指令打断：先把该桶结算完，再放行指令。

        Args:
            key: 桶键。
        """
        rnd = self.store.active.get(key)
        if rnd is not None:
            self.store.begin_finalize(rnd)
            rnd.flush.set()
        if not self.store.finalizing_rounds(key):
            return
        self.stats.add_interrupt()
        try:
            await asyncio.wait_for(self.store.wait_idle(key),
                                   timeout=float(self.cfg.interrupt_wait))
        except asyncio.TimeoutError:
            pending = self.store.finalizing_rounds(key)
            self.stats.add_interrupt(timeout=True)
            log_event("aggregate.interrupt_timeout", key=key, pending=len(pending))
            for extra in pending:
                self.store.abort(extra, cause="interrupt_timeout")

    # ------------------------------------------------------------------ 杂项
    def _accepts(self, event: AstrMessageEvent) -> bool:
        """是否处理该事件。

        Args:
            event: 消息事件。

        Returns:
            True 表示参与聚合。
        """
        if not self.cfg.group_enabled and event_io.channel_of(event) == "group":
            return False
        return True

    def _silence(self, event: Any, cause: str | None = None) -> None:
        """唯一静默入口。

        `cause=None` 表示**常规静默、不计数**——`on_event` 里"非 Owner 事件静默退出"
        是聚合的正常路径（每条补充消息都会走一次）。把它计入 `drop_total`，
        计数就会被消息条数撑爆，"丢弃"这个指标当场失去意义。

        Args:
            event: 事件。
            cause: 丢弃原因；None 表示常规静默。
        """
        try:
            event_io.silence(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 静默失败（已忽略）：{exc}")
        if cause is not None:
            self.stats.add_drop(cause)
            log_event("aggregate.drop", umo=event_io.umo_of(event), cause=cause)

    def panic_release(self, event: AstrMessageEvent) -> None:
        """异常兜底：尽量保证事件不被卡住。

        Args:
            event: 事件。
        """
        try:
            key = event_io.key_of(event)
        except Exception:  # noqa: BLE001
            return
        rnd = self.store.active.get(key)
        if rnd is not None and rnd.owner_event is event:
            self.store.abort(rnd, cause="exception")
            self._silence(event, cause="failed")

    def _dispatch_snapshot(self, event: AstrMessageEvent) -> tuple[list[Any], int]:
        """取"这次派发计划里有哪些 handler"以及本插件在其中的位置。

        这是抢占检测从"猜"变成"可证"的关键：`activated_handlers` 由
        `WakingCheckStage` 在派发前写进事件，里面是**这次真的会被调用的** handler
        （已按优先级降序）。如果我们的 handler 不在里面，说明这条事件根本没被派发
        给我们（例如主动回复、其它插件直接 `request_llm()` 的合成事件、Agent 重放），
        不能叫"被抢占"——那正是 v0.9.0 判据（有内容 + 无标记 = 抢占）误报的来源。

        Args:
            event: 消息事件。

        Returns:
            `(handlers, self_index)`；`self_index` 为 -1 表示不在派发计划里。
        """
        if not self.message_handler_name:
            # 显式关闭抢占检测（集成方没有 handler 名的概念时用）。
            # 不能靠"空串去 index()"：万一某个 handler 的名字也是空的就会误判。
            return [], -1
        try:
            handlers = list(event.get_extra("activated_handlers") or [])
        except Exception:  # noqa: BLE001 - 老版本/测试替身可能没有 get_extra
            return [], -1
        names = [str(getattr(handler, "handler_name", "") or "") for handler in handlers]
        try:
            index = names.index(self.message_handler_name)
        except ValueError:
            index = -1
        return handlers, index

    def _should_have_handled(self, event: AstrMessageEvent) -> bool:
        """判断"这条消息本来该由本插件处理"（抢占检测用，刻意保守）。

        必要条件：本插件的消息 handler **在这次的 `activated_handlers` 里**。
        合成事件没有这份派发计划，或计划里没有我们，都不能计为抢占——否则会把
        主动回复、其它插件发起的 LLM 请求报成故障，**误报比漏报更糟**：
        它会让人在两天内把这个机制关掉。

        其余排除项：群聊关闭、空消息、指令、`input_status`/撤回通知。

        Args:
            event: 消息事件。

        Returns:
            True 表示如果没被抢占，本插件本应处理它。
        """
        _handlers, index = self._dispatch_snapshot(event)
        if index < 0:
            return False
        # 插件整体关闭 / 正在卸载时，`handle` 本来就会在入口直接返回；
        # 但 handler 仍注册在派发计划里（注册发生在 import 时），不排除就会
        # 把"用户主动关掉插件"刷成满屏 aggregate.preempted。
        if not self.cfg.enabled or self._shutting_down:
            return False
        if caps_mod.is_typing_notice(event) or caps_mod.is_recall_notice(event):
            return False
        if not self._accepts(event):
            return False
        text = str(getattr(event, "message_str", "") or "")
        if event_io.is_command(text, self.cfg.command_prefixes):
            return False
        try:
            return event_io.parse_incoming(event, self.cfg).has_content()
        except Exception:  # noqa: BLE001
            return False

    def check_preempted(self, event: AstrMessageEvent) -> bool:
        """抢占检测：本该处理却没处理到，说明被更高优先级者挤掉了。

        **不做任何补救动作**（补救会和对方抢，反而更乱），只做三件事：告警、计数、
        给出**可证伪的证据**。告警里带上派发计划总数、本插件的位置、以及紧挨在
        前面最多 3 个 handler，下一次不必再猜是谁挡的。

        Args:
            event: 消息事件。

        Returns:
            True 表示这次确实判定为被抢占（调用方可据此落 trace 证据）。
        """
        if getattr(event, MARKER_ATTR, False):
            return False
        if not self._should_have_handled(event):
            return False
        handlers, index = self._dispatch_snapshot(event)
        prior: list[str] = []
        for handler in handlers[:index][-3:]:
            priority = (getattr(handler, "extras_configs", None) or {}).get(
                "priority", "?"
            )
            prior.append(f"{getattr(handler, 'handler_name', '?')}@{priority}")
        # 打上标记，避免同一事件因多次钩子调用重复告警
        try:
            setattr(event, MARKER_ATTR, True)
        except Exception:  # noqa: BLE001
            pass

        umo = event_io.umo_of(event)
        self.stats.add_preempted()
        log_event(
            "aggregate.preempted",
            umo=umo,
            priority=MESSAGE_PRIORITY,
            dispatched=len(handlers),
            position=index,
            msg_id=getattr(getattr(event, "message_obj", None), "message_id", "?"),
            prior=",".join(prior) or "(none)",
            hint=(
                "本条消息未进入聚合：派发计划里有本插件，但入口没被执行"
                "（通常是排在前面的 handler 静默了事件）。"
                "prior= 是紧挨在本插件前面的执行者；若 position=0 仍被跳过，"
                "说明事件在派发循环之外被消费"
            ),
        )
        return True

    def state_rows(self) -> list[dict[str, Any]]:
        """页面用的状态行（转发到 observability，保持页面只依赖一个入口）。

        Returns:
            行字典列表。
        """
        return snapshot_state(self.store)

    def apply_preset(self, name: str) -> dict[str, Any]:
        """切换预设并落到运行中的配置上，同时**写回插件配置**。

        只改内存的话，页面显示 `patient`，重载插件/重启 AstrBot 后又变回 `balanced`，
        配置表单里也一直是旧值（现场踩过）。写回失败不影响本次生效，只记 warning。

        Args:
            name: 预设名（已由调用方校验）。

        Returns:
            生效值摘要。
        """
        from .config import PRESETS, PRESET_NAMES, apply_preset as _apply, validate

        if name not in PRESET_NAMES:
            raise ValueError(f"unknown preset: {name}")
        self._install_config(validate(_apply(_replace_preset(self.cfg, name))))
        effective = dict(PRESETS[name])
        logger.info(f"[{PLUGIN_NAME}] 预设已切换为 {name}：{effective}")

        # 写回"显式值"而不是只写 preset：这样即使以后调整了预设表，
        # 用户此刻的窗口也不会被悄悄改掉（与话题路由器 v0.9.3 一致）。
        applied: dict[str, Any] = {
            key: getattr(self.cfg, key) for key in PRESETS[name]
        }
        applied["preset"] = self.cfg.preset
        persist = self.persist_preset
        if callable(persist):
            try:
                persist(applied)
            except Exception as exc:  # noqa: BLE001 - 写配置失败不能影响聚合
                logger.warning(
                    f"[{PLUGIN_NAME}] 预设写回配置失败（本次仍生效）："
                    f"{type(exc).__name__}: {exc}"
                )

        return {
            "debounce_time": self.cfg.debounce_time,
            "adaptive": self.cfg.adaptive,
            "adaptive_max_wait": self.cfg.adaptive_max_wait,
            "adaptive_max_total_wait": self.cfg.adaptive_max_total_wait,
        }

    def apply_config(self, raw: dict[str, Any] | None, *, persist: bool = False) -> None:
        """热更新聚合配置（改了聚合配置后不必重载插件）。

        只重算 `Config` 并挂回各子模块：**进行中的轮次继续用旧值走完，新轮次用新值**，
        不打断用户正在等的回复。

        Args:
            raw: 配置补丁；`None` 表示按当前配置对象重算一遍。
            persist: 是否把补丁写回插件配置对象（页面接口用；默认只改内存）。
        """
        merged = self._config_snapshot()
        if isinstance(raw, dict):
            merged.update(raw)
        self._install_config(load_config(merged))
        # A3 的"留存"口径要跟着新配置重算（哈希不同会被忽略，不会误记）
        self.stats.track_config(stats_mod.config_hash(self.cfg))
        logger.info(f"[{PLUGIN_NAME}] 聚合配置已热更新 {describe_effective(self.cfg)}")

        if persist and isinstance(raw, dict):
            try:
                self._persist_config(dict(raw))
            except Exception as exc:  # noqa: BLE001 - 写配置失败不能影响聚合
                logger.warning(
                    f"[{PLUGIN_NAME}] 热更新配置写回失败（本次仍生效）："
                    f"{type(exc).__name__}: {exc}"
                )

    def _install_config(self, cfg: Config) -> None:
        """把一份新配置挂到所有使用方（唯一入口，避免某处漏更新）。

        Args:
            cfg: 已校验的配置。
        """
        self.cfg = cfg
        self.store.cfg = cfg
        self.stats.cfg = cfg
        self.attachments.cfg = cfg

    def _config_snapshot(self) -> dict[str, Any]:
        """当前原始配置的浅拷贝（热更新合并的基线）。

        Returns:
            配置字典；没有配置对象时返回空字典。
        """
        return dict(self._config_obj) if isinstance(self._config_obj, dict) else {}

    def _persist_config(self, values: dict[str, Any]) -> None:
        """把显式值写回插件配置对象并落盘。

        生产环境里 `config` 是 `AstrBotConfig`（dict 子类）且带同步的
        `save_config()`；测试/老版本没有这个方法时只更新内存，不报错。

        Args:
            values: 要写回的键值（只写显式生效值，不写默认值）。
        """
        if not isinstance(self._config_obj, dict):
            return
        self._config_obj.update(values)
        save = getattr(self._config_obj, "save_config", None)
        if callable(save):
            save()

    def _data_dir(self) -> str:
        """插件数据目录（优先 AstrBot 提供的路径，缺失则回退 ~/.astrbot）。

        Returns:
            目录路径。
        """
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = Path(get_astrbot_plugin_data_path())
        except Exception:  # noqa: BLE001 - 老版本/测试环境
            base = Path.home() / ".astrbot" / "data" / "plugin_data"
        return str(base / PLUGIN_NAME)


def _replace_preset(cfg: Config, name: str) -> Config:
    """只替换 preset 字段。

    Args:
        cfg: 配置。
        name: 预设名。

    Returns:
        新配置。
    """
    from dataclasses import replace

    return replace(cfg, preset=name)


def _dedupe(images: list[str]) -> list[str]:
    """按引用去重（保持首次出现顺序；不用内容哈希——那要额外读文件）。

    Args:
        images: 引用列表。

    Returns:
        去重后的列表。
    """
    seen: set[str] = set()
    out: list[str] = []
    for ref in images:
        if ref in seen:
            continue
        seen.add(ref)
        out.append(ref)
    return out
