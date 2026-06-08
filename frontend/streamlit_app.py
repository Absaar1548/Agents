"""Streamlit UI for the BRD agent.

Talks to the FastAPI backend over HTTP. Backend is the source of truth for
session state — every action refreshes the local mirror from the response.

Run from /home/azureuser/temp/PoC/brd_agent/:
    .venv/bin/streamlit run frontend/streamlit_app.py

Set BACKEND_URL env var to override (default: http://localhost:8010).
"""
from __future__ import annotations

import json
import os
import uuid

import httpx
import streamlit as st


BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8010")

st.set_page_config(page_title="BRD Agent", page_icon="📝", layout="centered")


# ----- HTTP helpers -----
def _client() -> httpx.Client:
    return httpx.Client(base_url=BACKEND_URL, timeout=120.0)


def api_chat(message: str, session_id: str) -> dict:
    with _client() as c:
        r = c.post("/chat", json={"message": message, "session_id": session_id})
        r.raise_for_status()
        return r.json()


def api_resume(action: str, session_id: str) -> dict:
    with _client() as c:
        r = c.post("/resume", json={"action": action, "session_id": session_id})
        r.raise_for_status()
        return r.json()


def api_generate(session_id: str) -> dict:
    with _client() as c:
        r = c.post("/generate-brd", json={"session_id": session_id})
        r.raise_for_status()
        return r.json()


def api_approve(session_id: str) -> dict:
    with _client() as c:
        r = c.post("/approve", json={"session_id": session_id})
        r.raise_for_status()
        return r.json()


def api_request_changes(feedback: str, session_id: str) -> dict:
    with _client() as c:
        r = c.post(
            "/request-changes",
            json={"feedback": feedback, "session_id": session_id},
        )
        r.raise_for_status()
        return r.json()


def api_reset(session_id: str) -> dict:
    with _client() as c:
        r = c.post("/reset", json={"session_id": session_id})
        r.raise_for_status()
        return r.json()


def api_health() -> dict:
    with _client() as c:
        r = c.get("/health")
        r.raise_for_status()
        return r.json()


# ----- session state -----
def init_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []  # list of {role, content}
    if "current_draft" not in st.session_state:
        st.session_state.current_draft = None
    if "approved_brd_id" not in st.session_state:
        st.session_state.approved_brd_id = None
    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid.uuid4().hex
    if "hitl_1" not in st.session_state:
        st.session_state.hitl_1 = None


init_state()


# ----- header -----
st.title("📝 BRD Agent")
st.caption(f"Backend: `{BACKEND_URL}` · Traces in Phoenix: http://localhost:6006")

col_reset, col_health = st.columns([1, 5])
with col_reset:
    if st.button("Reset session", use_container_width=True):
        res = api_reset(st.session_state.session_id)
        st.session_state.session_id = res["session_id"]
        st.session_state.messages = []
        st.session_state.current_draft = None
        st.session_state.approved_brd_id = None
        st.session_state.hitl_1 = None
        st.rerun()
with col_health:
    try:
        h = api_health()
        st.success(f"connected · agent {h['agent_id']}@{h['version']}", icon="✅")
    except Exception as e:
        st.error(f"backend unreachable: {e}", icon="🚫")
        st.stop()


# ----- approval banner if approved -----
if st.session_state.approved_brd_id:
    st.success(
        f"BRD approved · brd_id `{st.session_state.approved_brd_id}`. "
        "Click 'Reset session' to start a new one.",
        icon="✅",
    )


# ----- HITL 1 banner (when graph paused for user confirmation) -----
if st.session_state.hitl_1:
    hitl = st.session_state.hitl_1
    st.divider()
    st.markdown("### 📋 Ready for Production")
    summary = hitl.get("summary", {})
    st.markdown(f"**Title:** {summary.get('title', 'Untitled')}")
    objectives = summary.get("objectives", [])
    if objectives:
        st.markdown("**Objectives:**")
        for obj in objectives:
            st.markdown(f"- {obj}")
    open_questions = summary.get("open_questions", [])
    if open_questions:
        st.markdown(f"**Open questions:** {len(open_questions)}")
    st.markdown(f"**Feedback items:** {summary.get('feedback_items', 0)}")

    col_proceed, col_add = st.columns([1, 1])
    with col_proceed:
        if st.button("✅ Proceed to production", type="primary", use_container_width=True):
            res = api_resume("proceed", st.session_state.session_id)
            st.session_state.hitl_1 = None
            # Now generate the BRD
            res_gen = api_generate(st.session_state.session_id)
            draft = res_gen["draft"]
            st.session_state.current_draft = draft
            st.session_state.messages.append(
                {
                    "role": "brd_draft",
                    "brd_id": draft["brd_id"],
                    "content": draft,
                }
            )
            st.rerun()
    with col_add:
        if st.button("➕ Add more details", use_container_width=True):
            res = api_resume("add_more", st.session_state.session_id)
            st.session_state.hitl_1 = None
            reply = res.get("reply", "")
            if reply:
                st.session_state.messages.append(
                    {"role": "assistant", "content": reply}
                )
            st.rerun()


# ----- chat history -----
for msg in st.session_state.messages:
    if msg["role"] == "brd_draft":
        with st.chat_message("assistant"):
            st.markdown("**📄 BRD draft generated.** Review below:")
            with st.expander(f"Draft BRD · brd_id `{msg['brd_id']}`", expanded=True):
                st.json(msg["content"])
    else:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


# ----- approval controls (only when a draft exists and not yet approved) -----
if st.session_state.current_draft and not st.session_state.approved_brd_id:
    st.divider()
    st.markdown("### Review the draft above")
    col_approve, col_changes = st.columns([1, 3])
    with col_approve:
        if st.button("✅ Approve", type="primary", use_container_width=True):
            res = api_approve(st.session_state.session_id)
            st.session_state.approved_brd_id = res["brd_id"]
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": f"✅ BRD approved · brd_id `{res['brd_id']}`",
                }
            )
            st.rerun()
    with col_changes:
        with st.form("request_changes_form", clear_on_submit=True):
            feedback = st.text_input(
                "Request changes — what should change?",
                placeholder="e.g. Add a risk about regulatory audits.",
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button("Request changes")
            if submitted and feedback.strip():
                res = api_request_changes(
                    feedback.strip(), st.session_state.session_id
                )
                st.session_state.messages.append(
                    {"role": "user", "content": f"_Requested changes:_ {feedback}"}
                )
                st.session_state.messages.append(
                    {"role": "assistant", "content": res["reply"]}
                )
                # draft persists per the design — keep current_draft as-is.
                st.rerun()


# ----- Generate BRD button (when in gathering and no current draft awaiting) -----
if (
    not st.session_state.current_draft or st.session_state.approved_brd_id
) and not st.session_state.approved_brd_id:
    st.divider()
    if st.button("🪄 Generate BRD", use_container_width=True):
        res = api_generate(st.session_state.session_id)
        draft = res["draft"]
        st.session_state.current_draft = draft
        st.session_state.messages.append(
            {
                "role": "brd_draft",
                "brd_id": draft["brd_id"],
                "content": draft,
            }
        )
        st.rerun()
elif st.session_state.current_draft and not st.session_state.approved_brd_id:
    # Already have an awaiting-approval draft — offer a "regenerate" option
    # that overwrites the current draft.
    if st.button(
        "🔄 Regenerate BRD (replaces draft above)", use_container_width=True
    ):
        res = api_generate(st.session_state.session_id)
        draft = res["draft"]
        st.session_state.current_draft = draft
        st.session_state.messages.append(
            {
                "role": "brd_draft",
                "brd_id": draft["brd_id"],
                "content": draft,
            }
        )
        st.rerun()


# ----- chat input -----
if user_input := st.chat_input(
    "Tell me about your project...",
    disabled=bool(st.session_state.approved_brd_id) or bool(st.session_state.hitl_1),
):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                res = api_chat(user_input, st.session_state.session_id)
                # Phase 2: handle HITL 1 interrupt
                if res.get("mode") == "hitl_1":
                    st.session_state.hitl_1 = res.get("hitl")
                    st.rerun()
                else:
                    reply = res["reply"]
                    st.markdown(reply)
                    st.session_state.messages.append(
                        {"role": "assistant", "content": reply}
                    )
                    # If the backend transitioned out of awaiting_approval (the
                    # user implicitly requested changes via plain chat), keep
                    # the local draft visible — the response payload carries it.
                    if res.get("draft"):
                        st.session_state.current_draft = res["draft"]
            except Exception as e:
                st.error(f"backend error: {e}")
