"""固定指令通道：形如 ``/指令 参数`` 的纯文本消息由本地函数处理。

拦截点在 ``MessageHandler.on_event`` 中、媒体下载之前：一旦命中指令，
消息不会再下载附件，也不会进入 Agent 投递队列。

自动注册
--------
本模块中任何名为 ``cmd_<名字>`` 的模块级函数都会**自动**成为 ``/<名字>``
指令，不需要任何注册代码。名字取 ``cmd_`` 之后的部分并转小写：

    cmd_random  ->  /random
    cmd_ping    ->  /ping

分发时按命名空间实时解析，因此运行期往模块里追加（或替换）``cmd_*``
函数也会立即生效，无需重启。

函数可以带 ``__aliases__`` 属性指定别名::

    def cmd_info(ctx: CommandContext) -> str: ...
    cmd_info.__aliases__ = ("i", "about")

函数签名统一为 ``func(ctx: CommandContext) -> Optional[str]``，
返回值作为回复文本，返回空串表示「确认是这条指令但不回复」。

手动注册（``register()``）仍然保留，优先级高于自动发现。

约定
----
* 只有「纯信息部分」以 ``/`` 开头的消息才进入本通道。纯信息部分指
  ``IncomingMessage.content``——群聊里由 ``bridge`` 追加的 ``用户xxxx：``
  前缀不在其中，因此不会干扰指令识别。
* 指令格式为 ``/指令名 参数``，指令名与参数用空白分隔，参数可省略。
* 统一入口是 ``CommandChannel.method()``，返回值约定：

  - ``None``：不是指令（首字符不是 ``/``），调用方按普通消息继续处理
  - ``str`` ：是指令。返回需要回复的文本；空串表示不回复
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Optional

if TYPE_CHECKING:  # 仅用于类型注解，避免与 handler 循环导入
    from .handler import IncomingMessage

log = logging.getLogger(__name__)

COMMAND_PREFIX = "/"

# 自动发现的函数名前缀：cmd_xxx -> /xxx
CMD_FUNC_PREFIX = "cmd_"

# 别名属性名
ALIAS_ATTR = "__aliases__"

# /random 的默认取值区间
RANDOM_LOW = 1
RANDOM_HIGH = 100

RANDOM_USAGE = "用法：/random [下限] [上限]"


@dataclass
class CommandContext:
    """一次指令调用的上下文。"""

    name: str                                       # 指令名（小写，不含 /）
    args: str                                       # 参数原文（已 strip）
    argv: list[str] = field(default_factory=list)   # 参数按空白切分
    text: str = ""                                  # 完整原始文本
    message: Optional["IncomingMessage"] = None     # 来源消息


CommandFunc = Callable[[CommandContext], Optional[str]]


def parse_command(text: str) -> Optional[tuple[str, str]]:
    """把 ``/指令 参数`` 拆成 ``(指令名, 参数)``。

    - 首字符（去空白后）不是 ``/``：返回 ``None``，调用方按普通消息处理
    - 只有 ``/`` 没有指令名：返回 ``("", "")``，由调用方回未知指令提示
    - 其余情况：指令名转小写，参数为剩余部分（可能为空串）
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith(COMMAND_PREFIX):
        return None
    body = stripped[len(COMMAND_PREFIX):].strip()
    if not body:
        return ("", "")
    parts = body.split(None, 1)
    return (parts[0].lower(), parts[1].strip() if len(parts) > 1 else "")


def _to_int(text: str) -> Optional[int]:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _normalize(name: str) -> str:
    """指令名规范化：去掉前导 / 与空白，转小写。"""
    return (name or "").strip().lstrip(COMMAND_PREFIX).strip().lower()


def _aliases_of(func: CommandFunc) -> tuple[str, ...]:
    """读取函数上的 ``__aliases__``，返回规范化后的别名元组。"""
    raw = getattr(func, ALIAS_ATTR, ())
    if isinstance(raw, str):
        raw = (raw,)
    try:
        items = list(raw or ())
    except TypeError:
        return ()
    result: list[str] = []
    for item in items:
        key = _normalize(str(item))
        if key and key not in result:
            result.append(key)
    return tuple(result)


class CommandChannel:
    """指令表 + 分发入口。

    指令来源两处，按优先级：

    1. ``register()`` 手动注册（优先级高，可覆盖同名自动指令）
    2. 绑定命名空间中的 ``cmd_*`` 函数（自动发现，实时解析）
    """

    def __init__(self, namespace: Optional[Mapping[str, Any]] = None) -> None:
        self._handlers: dict[str, CommandFunc] = {}
        # 保持对命名空间的引用（不复制），这样后续新增的函数也能被发现
        self._namespace: Mapping[str, Any] = (
            namespace if namespace is not None else {}
        )

    # ---------------- 绑定 ----------------

    def bind(self, namespace: Mapping[str, Any]) -> "CommandChannel":
        """绑定用于自动发现的命名空间（通常是模块的 ``globals()``）。"""
        self._namespace = namespace
        return self

    # ---------------- 手动注册 ----------------

    def register(
        self,
        name: str,
        func: CommandFunc,
        aliases: tuple[str, ...] = (),
    ) -> None:
        """手动注册一条指令（可带别名），优先级高于自动发现。"""
        key = _normalize(name)
        if not key:
            raise ValueError("指令名不能为空")
        self._handlers[key] = func
        for alias in aliases:
            alias_key = _normalize(alias)
            if alias_key and alias_key != key:
                self._handlers[alias_key] = func

    # ---------------- 自动发现 ----------------

    def _iter_auto(self) -> Iterator[tuple[str, CommandFunc]]:
        """遍历命名空间中的 ``cmd_*`` 可调用对象。"""
        for attr, value in list(self._namespace.items()):
            if not attr.startswith(CMD_FUNC_PREFIX) or not callable(value):
                continue
            name = attr[len(CMD_FUNC_PREFIX):].strip().lower()
            if name:
                yield name, value

    def discover(self) -> dict[str, CommandFunc]:
        """当前生效的指令表：自动发现 + 手动注册（后者覆盖），含别名。"""
        table: dict[str, CommandFunc] = {}
        for name, func in self._iter_auto():
            table[name] = func
        table.update(self._handlers)
        # 别名放在最后，且不覆盖已有的真实指令名
        for name, func in list(table.items()):
            for alias in _aliases_of(func):
                table.setdefault(alias, func)
        return table

    # ---------------- 查询 ----------------

    @property
    def names(self) -> list[str]:
        """当前可用的指令名（排序后）。"""
        return sorted(self.discover())

    def is_command(self, text: str) -> bool:
        return parse_command(text) is not None

    # ---------------- 分发 ----------------

    def method(
        self,
        text: str,
        message: Optional["IncomingMessage"] = None,
    ) -> Optional[str]:
        """固定指令通道入口。

        返回 ``None`` 表示不是指令；返回字符串表示这条消息已被指令通道接管，
        内容非空时作为回复发出。
        """
        parsed = parse_command(text)
        if parsed is None:
            return None

        name, args = parsed
        raw = (text or "").strip()
        if not name:
            return self._unknown("", raw)

        handler = self.discover().get(name)
        if handler is None:
            return self._unknown(name, raw)

        ctx = CommandContext(
            name=name,
            args=args,
            argv=args.split(),
            text=raw,
            message=message,
        )
        log.info("执行指令 /%s 参数=%r", name, args)
        try:
            return handler(ctx)
        except Exception:
            log.exception("指令 /%s 执行失败：%r", name, raw[:100])
            return "指令执行出错，请稍后重试"

    def _unknown(self, name: str, raw: str) -> str:
        log.info("未知指令：%r", raw[:100])
        available = "、".join("/" + n for n in self.names) or "（暂未注册）"
        shown = ("/" + name) if name else "/"
        return "未知指令 %s，可用指令：%s" % (shown, available)


# ---------------- 指令实现 ----------------
#
# 这里只需按 cmd_<名字> 命名，就会被自动发现为 /<名字> 指令。


def cmd_random(ctx: CommandContext) -> str:
    """抽一个随机整数。

    - ``/random``       取 1~100
    - ``/random 10``    取 1~10
    - ``/random 5 10``  取 5~10（下限大于上限时自动交换）
    """
    argv = ctx.argv
    if not argv:
        low, high = RANDOM_LOW, RANDOM_HIGH
    elif len(argv) == 1:
        parsed = _to_int(argv[0])
        if parsed is None:
            return "参数不是整数：%s。%s" % (argv[0], RANDOM_USAGE)
        low, high = RANDOM_LOW, parsed
    else:
        low, high = _to_int(argv[0]), _to_int(argv[1])
        if low is None or high is None:
            return "参数不是整数：%s %s。%s" % (argv[0], argv[1], RANDOM_USAGE)

    if low > high:
        low, high = high, low
    value = random.randint(low, high)
    log.info("随机数：区间 %d~%d -> %d", low, high, value)
    return "随机数：%d（范围 %d~%d）" % (value, low, high)

# ---------------- 默认指令表 ----------------

# 绑定本模块命名空间：之后新增的 cmd_* 函数会被自动发现
_default = CommandChannel()
_default.bind(globals())


def method(
    text: str,
    message: Optional["IncomingMessage"] = None,
) -> Optional[str]:
    """默认指令表的入口，供 ``MessageHandler`` 直接调用。"""
    return _default.method(text, message)


def register(
    name: str,
    func: CommandFunc,
    aliases: tuple[str, ...] = (),
) -> None:
    """向默认指令表手动注册指令（优先级高于自动发现）。"""
    _default.register(name, func, aliases)


def discover() -> dict[str, CommandFunc]:
    """默认指令表当前生效的全部指令。"""
    return _default.discover()


def names() -> list[str]:
    """默认指令表当前可用的指令名。"""
    return _default.names