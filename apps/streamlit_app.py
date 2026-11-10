"""Streamlit 界面入口：展示聊天和登录状态，业务操作交给工具与会话层。

启动：python -m streamlit run apps/streamlit_app.py
"""
from pathlib import Path
from datetime import datetime
from dataclasses import replace
from inspect import signature
from zoneinfo import ZoneInfo
import time

import streamlit as st

from oa_agent.agents.oa_agent import build_agent
from oa_agent.agents.streaming import stream_agent_response
from oa_agent.config import load_settings
from oa_agent.history.manager import ConversationHistory
from oa_agent.history.mysql_repository import MySQLHistoryRepository
from oa_agent.sessions.session import LoginSession
from oa_agent.sessions.tasks import ChatTaskRunner
from oa_agent.tools.login import make_login_tool
from oa_agent.tools.employee import make_employee_tool
from oa_agent.tools.attendance import make_attendance_tools

# 按脚本位置定位项目 .env，兼容从 PyCharm 或其他工作目录启动。
settings = load_settings(Path(__file__).resolve().parents[1] / ".env")
st.set_page_config(page_title="OA 智能助手", page_icon="🏢")
st.title("OA 智能助手")
st.caption("在本机浏览器中登录 OA，再通过对话使用工具。")
if "oa_session" not in st.session_state:
    # Streamlit 每次交互都会重跑脚本，用 session_state 保留当前用户的浏览器线程。
    st.session_state.oa_session = LoginSession(settings.login, settings.database)
if "chat_tasks" not in st.session_state:
    history = ConversationHistory(settings.login.username, MySQLHistoryRepository(settings.database))
    history.refresh()
    st.session_state.chat_tasks = ChatTaskRunner(history)
chat_tasks = st.session_state.chat_tasks
history = chat_tasks.history
session = st.session_state.oa_session
login_tool = make_login_tool(session)
employee_tool = make_employee_tool(session)
attendance_tools = make_attendance_tools(session)


def run_login_action(action):
    """将登录操作的结果转成界面通知，后台认证不依赖该通知是否被展示。"""
    try:
        result = action()
        kind = "error" if result["state"] == "error" else (
            "success" if result["state"] == "verified" else "info"
        )
        st.session_state.login_notice = (kind, result["message"])
    except Exception:
        # 浏览器异常可能包含带凭据的 SSO 地址，界面只显示固定提示。
        st.session_state.login_notice = (
            "error", "操作未完成，请检查 OA 网络、Chromium 安装及登录检测配置。"
        )


@st.fragment(run_every=1)
def render_login_panel():
    """每秒独立刷新侧栏；实际登录检测在后台线程进行，不重跑整段聊天流程。"""
    st.subheader("OA 会话")
    st.write("地址：https://soa.com.cn/")
    st.caption("浏览器会在运行 Streamlit 的电脑上打开。")
    if not settings.database.configured:
        st.info("请在本机 .env 配置 MYSQL_USER、MYSQL_PASSWORD 和 SESSION_ENCRYPTION_KEY。")
    if st.button("打开 OA 登录", use_container_width=True):
        run_login_action(lambda: login_tool.invoke({"action": "open"}))
    if st.button("关闭会话并清除凭据", use_container_width=True):
        run_login_action(lambda: session.call("clear"))
    if "login_notice" in st.session_state:
        kind, message = st.session_state.login_notice
        getattr(st, kind)(message)
    try:
        # 这里只读取状态和数量；凭据原文保留在会话层，不写入页面或对话。
        status = session.call("status")
        st.write(f"状态：{status['state']}")
        st.write(f"Cookie：{status['cookie_count']} 项；Token：{status['token_count']} 项")
        if status.get("persistence_error"):
            st.error(status["message"])
        elif status.get("recovering"):
            st.info("正在恢复登录，完成浏览器验证后会自动继续当前任务。")
        elif status["state"] == "verified":
            st.success("登录成功，已自动采集会话。")
        elif status["state"] == "waiting_login":
            auto_messages = {
                "waiting_form": "已启用自动登录，正在等待登录表单就绪。",
                "submitted": "已自动提交账号密码，正在等待登录结果。",
                "needs_verification": "需要二次验证，请在浏览器中完成，随后自动采集会话。",
                "failed": "自动登录未完成，已停止自动重试。请在浏览器检查，或关闭会话后重试。",
            }
            st.info(auto_messages.get(status.get("auto_login"), "等待浏览器登录，成功后自动采集会话。"))
        if status["state"] == "verified" and not status["token_count"]:
            st.info("未发现独立 Token。OA 可能只使用 Cookie，或需配置实际 Token 字段。")
    except Exception:
        st.info("会话不可用，请重新打开登录。")


def render_history_panel():
    view_revision = history.revision
    st.subheader("历史会话")
    if st.button("新建会话", key="new_conversation", use_container_width=True):
        history.new()
        st.session_state.pop("delete_history_id", None)
        st.rerun()
    if history.warning:
        st.warning(history.warning)
    if history.persistent and st.button("刷新并重试保存", key="retry_history", use_container_width=True):
        history.retry(force=True)
    summaries = history.summaries
    if not summaries:
        st.caption("暂无历史会话，发送第一条消息后自动保存。")
    for summary in summaries:
        running = chat_tasks.busy and chat_tasks.running_conversation_id == summary.id
        selected = summary.id == history.current_id
        title = ("⏳ " if running else "● " if selected else "") + summary.title
        updated = datetime.fromtimestamp(summary.updated_at, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
        open_column, delete_column = st.columns([5, 1])
        if open_column.button(title, key=f"open_{summary.id}", help=f"最近更新：{updated}（上海时间）",
                              use_container_width=True):
            history.select(summary.id)
            st.session_state.pop("delete_history_id", None)
            st.rerun()
        if delete_column.button("删除", key=f"delete_{summary.id}", disabled=running,
                                help="任务执行中的会话暂时不能删除"):
            st.session_state.delete_history_id = summary.id
    if history.has_more and st.button("加载更多", key="more_history", use_container_width=True):
        history.load_more()
        st.rerun()
    deletion = st.session_state.get("delete_history_id")
    if deletion:
        st.warning("确定删除该会话及全部聊天记录？")
        confirm_column, cancel_column = st.columns(2)
        if confirm_column.button("确认删除", key="confirm_delete_history",
                                 disabled=chat_tasks.busy and deletion == chat_tasks.running_conversation_id):
            if chat_tasks.delete_conversation(deletion):
                st.session_state.pop("delete_history_id", None)
                history.refresh()
            st.rerun()
        if cancel_column.button("取消", key="cancel_delete_history"):
            st.session_state.pop("delete_history_id", None)
            st.rerun()
    # 使用渲染开始时的版本，避免漏掉渲染过程中后台完成的保存。
    st.session_state.history_view_revision = view_revision


with st.sidebar:
    render_login_panel()
    render_history_panel()
    st.divider()
    st.subheader("思考展示")
    st.toggle("请求模型推理摘要", value=settings.model.reasoning_summary, key="request_reasoning_summary",
              help="需要模型服务支持。开启后会请求推理摘要；若接口不支持，请关闭。")


def render_execution(progress):
    """思考文本随流出现，工具记录穿插其中，完成后自动收起。"""
    elapsed = (progress.finished_at or time.monotonic()) - progress.started_at
    running = progress.state == "running"
    label = "思考中" if running else "思考已中断" if progress.state == "error" else "已思考"
    # 新版 Streamlit 提供轻量的行内样式；旧版仍使用相同的展开/收起行为。
    style = {"type": "compact"} if "type" in signature(st.status).parameters else {}
    status = st.status(f"{label} · {elapsed:.1f} 秒", state=progress.state, expanded=running, **style)
    with status.container():
        if progress.summary:
            st.caption("模型提供的推理摘要与操作说明")
        steps = {step.key: step for step in progress.steps}
        for entry in progress.thinking:
            if entry.kind in {"summary", "commentary"}:
                st.markdown(entry.text)
            elif entry.key in steps:
                step = steps[entry.key]
                icon = {"running": "⏳", "complete": "✓", "error": "⚠"}[step.state]
                duration = (step.finished_at or time.monotonic()) - step.started_at
                st.caption(f"{icon} {step.text} · {duration:.1f} 秒")
        if not progress.thinking:
            st.caption("正在等待模型输出…" if running else "本轮未收到模型推理摘要。")
        elif not progress.summary and not running and st.session_state.request_reasoning_summary:
            st.caption("本轮未收到模型推理摘要。")


@st.fragment(run_every=0.3)
def render_conversation():
    # 页面重跑只读取进度；原 Agent 调用和业务任务保留在后台执行。
    completed = chat_tasks.poll()
    history.retry()
    if completed or history.revision != st.session_state.history_view_revision:
        st.rerun()
    messages = chat_tasks.messages
    progress_by_index = {progress.user_index + 1: progress for progress in chat_tasks.progress()}
    for index, message in enumerate(messages):
        with st.chat_message(message["role"]):
            if index in progress_by_index and message["role"] == "assistant":
                render_execution(progress_by_index[index])
            st.markdown(message["content"])
    pending = progress_by_index.get(len(messages))
    if pending:
        with st.chat_message("assistant"):
            render_execution(pending)
            if pending.answer:
                st.markdown(pending.answer)
    if chat_tasks.busy:
        st.info("后台任务正在执行，可以切换查看或新建会话，完成后可发送新问题。若需要验证码，请在浏览器完成验证。")
    elif chat_tasks.messages and chat_tasks.messages[-1]["role"] == "user":
        st.warning("上次任务未完成或结果尚未保存。系统不会自动重复执行；涉及提交时请先在 OA 核实结果。")
    prompt = st.chat_input("例如：查询我的用户信息，或查询本月异常考勤", disabled=chat_tasks.busy)
    if prompt:
        if not settings.model.configured:
            st.info("请先在 .env 配置 OPENAI_API_KEY 和 OPENAI_MODEL；侧栏登录功能无需模型即可使用。")
            return

        def respond(messages, emit, tools=[login_tool, employee_tool, *attendance_tools],
                    model_config=replace(settings.model, reasoning_summary=st.session_state.request_reasoning_summary),
                    login_session=session):
            return stream_agent_response(build_agent(tools, model_config), messages, emit, login_session)

        chat_tasks.submit(prompt, respond, streaming=True)
        st.rerun()


render_conversation()
