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

import httpx
import streamlit as st


BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8010")

st.set_page_config(page_title="BRD Agent", page_icon="📝", layout="centered")


# ----- HTTP helpers -----
def _client() -> httpx.Client:
    return httpx.Client(base_url=BACKEND_URL, timeout=120.0)


def api_chat(message: str) -> dict:
    with _client() as c:
        r = c.post("/chat", json={"message": message})
        r.raise_for_status()
        return r.json()


def api_generate() -> dict:
    with _client() as c:
        r = c.post("/generate-brd")
        r.raise_for_status()
        return r.json()


def api_approve() -> dict:
    with _client() as c:
        r = c.post("/approve")
        r.raise_for_status()
        return r.json()


def api_request_changes(feedback: str) -> dict:
    with _client() as c:
        r = c.post("/request-changes", json={"feedback": feedback})
        r.raise_for_status()
        return r.json()


def api_reset() -> dict:
    with _client() as c:
        r = c.post("/reset")
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


init_state()


# ----- header -----
st.title("📝 BRD Agent")
st.caption(f"Backend: `{BACKEND_URL}` · Traces in Phoenix: http://localhost:6006")

col_reset, col_health = st.columns([1, 5])
with col_reset:
    if st.button("Reset session", use_container_width=True):
        api_reset()
        st.session_state.messages = []
        st.session_state.current_draft = None
        st.session_state.approved_brd_id = None
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
            res = api_approve()
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
                res = api_request_changes(feedback.strip())
                st.session_state.messages.append(
                    {"role": "user", "content": f"_Requested changes:_ {feedback}"}
                )
                st.session_state.messages.append(
                    {"role": "assistant", "content": res["reply"]}
                )
                # draft persists per the design — keep current_draft as-is.
                st.rerun()


# ----- Generate BRD button (when in gathering and no current draft awaiting) -----
if (not st.session_state.current_draft or st.session_state.approved_brd_id) and not st.session_state.approved_brd_id:
    st.divider()
    if st.button("🪄 Generate BRD", use_container_width=True):
        res = api_generate()
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
    if st.button("🔄 Regenerate BRD (replaces draft above)", use_container_width=True):
        res = api_generate()
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
    disabled=bool(st.session_state.approved_brd_id),
):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                res = api_chat(user_input)
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
