# -*- coding: utf-8 -*-
"""
astrbot_plugin_channel_awareness - 频道感知 + 搜索 + 总结 + 用户信息

对齐类脑娘 Discord 能力：
1. on_llm_request 注入频道上下文
2. /搜索 + AI工具 → 跨频道搜消息
3. /总结 + AI工具 → 频道消息总结
4. /用户信息 + AI工具 → Discord 用户信息查询
"""

import re
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

SEARCH_LIMIT = 50
MAX_RESULTS = 5
MAX_CHANNELS = 10
SUMMARY_LIMIT = 30  # 总结时读取的消息数


class ChannelAwareness(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # ========== 频道感知 ==========

    @filter.on_llm_request()
    async def inject_channel_context(self, event: AstrMessageEvent, req):
        location = self._get_location(event)
        if location and hasattr(req, "system_prompt") and req.system_prompt:
            req.system_prompt += f"\n[频道感知] 当前对话发生在: {location}\n"

    def _get_location(self, event: AstrMessageEvent) -> str:
        raw = event.message_obj.raw_message if event.message_obj else None
        if raw is None: return ""
        try:
            ch = getattr(raw, "channel", None)
            if ch is None: return ""
            cn = getattr(ch, "name", "")
            g = getattr(ch, "guild", None) or getattr(raw, "guild", None)
            gn = getattr(g, "name", "") if g else ""
            if hasattr(ch, "parent") and ch.parent:
                return f"服务器「{gn}」→ 论坛「{getattr(ch.parent,'name','')}」→ 帖子「{cn}」"
            elif gn: return f"服务器「{gn}」→ 频道「{cn}」"
            elif cn: return f"频道「{cn}」"
            else: return "私信"
        except Exception: return ""

    # ========== 跨频道搜索 ==========

    @filter.llm_tool(name="search_channel_messages")
    async def search_tool(self, event: AstrMessageEvent, keyword: str) -> str:
        """在服务器各频道搜索包含关键词的历史消息，返回消息链接。Args: keyword(string): 搜索关键词"""
        results = await self._search(event, keyword)
        return "\n".join(results) if results else "没有在服务器中找到包含该关键词的消息。"

    @filter.command("搜索")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        if not keyword.strip():
            yield event.plain_result("想找什么？告诉我关键词～\n例如: /搜索 角色卡")
            return
        yield event.plain_result("  正在搜...")
        results = await self._search(event, keyword)
        yield event.plain_result("\n".join(results[:MAX_RESULTS]) if results else f"没找到「{keyword}」")

    async def _search(self, event: AstrMessageEvent, keyword: str) -> list:
        raw = event.message_obj.raw_message if event.message_obj else None
        if not raw: return []
        guild = getattr(raw, "guild", None)
        if not guild: return ["此功能仅在服务器中可用。"]
        kw = keyword.lower()
        results, cc = [], 0
        for ch in guild.text_channels:
            if cc >= MAX_CHANNELS or len(results) >= MAX_RESULTS: break
            me = getattr(guild, "me", None)
            if me and hasattr(ch, "permissions_for") and not ch.permissions_for(me).read_message_history: continue
            try:
                async for msg in ch.history(limit=SEARCH_LIMIT):
                    content = getattr(msg, "content", "")
                    if kw in content.lower() and not msg.author.bot:
                        preview = content[:100].replace("\n", " ")
                        link = f"https://discord.com/channels/{guild.id}/{ch.id}/{msg.id}"
                        results.append(f"📌 **#{ch.name}** — {msg.author.display_name}:\n   \"{preview}{'...' if len(content)>100 else ''}\"\n   {link}")
                        if len(results) >= MAX_RESULTS: break
                cc += 1
            except Exception: continue
        if results: results.insert(0, f"🔍 搜索「{keyword}」:")
        return results

    # ========== 频道总结 ==========

    @filter.llm_tool(name="summarize_channel")
    async def summarize_tool(self, event: AstrMessageEvent) -> str:
        """总结当前频道最近的消息内容。当用户说「总结一下」「最近在聊什么」时调用。"""
        summary = await self._summarize(event)
        return summary

    @filter.command("总结")
    async def cmd_summary(self, event: AstrMessageEvent):
        yield event.plain_result("  正在读最近的消息...")
        summary = await self._summarize(event)
        yield event.plain_result(summary)

    async def _summarize(self, event: AstrMessageEvent) -> str:
        raw = event.message_obj.raw_message if event.message_obj else None
        if not raw: return "无法获取频道信息。"
        ch = getattr(raw, "channel", None)
        if not ch: return "无法获取当前频道。"
        try:
            messages = []
            async for msg in ch.history(limit=SUMMARY_LIMIT):
                content = getattr(msg, "content", "")
                if content and not msg.author.bot:
                    messages.append(f"{msg.author.display_name}: {content}")
            messages.reverse()
            if not messages: return "最近没有消息。"
            text = "\n".join(messages)
            umo = event.unified_msg_origin
            pid = await self.context.get_current_chat_provider_id(umo=umo)
            if pid:
                resp = await self.context.llm_generate(
                    chat_provider_id=pid,
                    prompt=f"请用简短的语言总结以下聊天内容（100字以内），让没有参与聊天的人也能快速了解大家在聊什么：\n\n{text}"
                )
                if resp and resp.completion_text:
                    return f"📋 最近 {SUMMARY_LIMIT} 条消息的总结:\n{resp.completion_text.strip()}"
            return "AI 总结暂时不可用。"

        except Exception as e:
            logger.error(f"[ChannelAwareness] 总结失败: {e}")
            return "总结失败，请稍后再试。"

    # ========== 用户信息 ==========

    @filter.llm_tool(name="get_user_profile")
    async def profile_tool(self, event: AstrMessageEvent) -> str:
        """获取当前发消息用户的 Discord 个人信息（用户名、加入时间、角色等）。"""
        info = self._get_user_profile(event)
        return info

    @filter.command("用户信息")
    async def cmd_user_info(self, event: AstrMessageEvent):
        """查看当前用户的 Discord 个人信息"""
        info = self._get_user_profile(event)
        yield event.plain_result(info)

    def _get_user_profile(self, event: AstrMessageEvent) -> str:
        raw = event.message_obj.raw_message if event.message_obj else None
        if not raw: return "无法获取用户信息。"
        author = getattr(raw, "author", None)
        if not author: return "无法获取用户信息。"

        lines = [f"👤 **{author.display_name}**"]
        lines.append(f"用户名: {author.name}#{author.discriminator}" if hasattr(author, "discriminator") and author.discriminator != "0" else f"用户名: {author.name}")
        lines.append(f"ID: {author.id}")

        if hasattr(author, "created_at"):
            lines.append(f"账号创建: {author.created_at.strftime('%Y-%m-%d')}")

        member = getattr(raw, "author", None)
        guild = getattr(raw, "guild", None)
        if guild and member:
            if hasattr(member, "joined_at") and member.joined_at:
                lines.append(f"加入服务器: {member.joined_at.strftime('%Y-%m-%d')}")
            if hasattr(member, "roles"):
                roles = [r.name for r in member.roles if r.name != "@everyone"]
                if roles:
                    lines.append(f"身份组: {', '.join(roles[:5])}")

        if hasattr(author, "avatar") and author.avatar:
            lines.append(f"头像: {author.avatar.url}")

        return "\n".join(lines)

    async def terminate(self):
        logger.info("[ChannelAwareness] 插件已卸载")
