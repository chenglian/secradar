import streamlit as st
import boto3
import hashlib
import hmac
import json
import os
import re
import uuid
import time

# --- CONSTANTS & S3 CONFIG ---
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "che-sec-filings")
TARGET_TICKERS_KEY = "reference-data/target-tickers.json"
CIK_MAPPING_KEY = "reference-data/company_tickers.json"
DEFAULT_LAMBDA_NAME = os.environ.get("INGESTION_LAMBDA_NAME", "secfiling-downloader")
DEFAULT_ADMIN_PASSWORD_HASH = "438156b54c653c730ad62964c10db3942ea161d9a3249f12aec3c50cbc92315a"

MODEL_OPTIONS = {
    "Claude Sonnet 4.6 (Deep Analyst — Recommended)": "global.anthropic.claude-sonnet-4-6",
    "Amazon Nova Pro (Balanced Cost & Quality)": "us.amazon.nova-pro-v1:0",
    "Claude Haiku 4.5 (Fast & Economy)": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="SecRadar AI", page_icon="📊", layout="centered")
st.title("📊 SecRadar: SEC AI Analyst")
st.markdown(
    "A Dual-Engine RAG combining **Athena SQL** for quantitative math and **Bedrock** for qualitative narrative risks."
)


# --- AWS SESSION & CLIENTS ---
def get_aws_session():
    """Creates a boto3 Session from Streamlit Cloud Secrets or falls back to local AWS credentials."""
    try:
        if "AWS_ACCESS_KEY_ID" in st.secrets and "AWS_SECRET_ACCESS_KEY" in st.secrets:
            return boto3.Session(
                aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
                aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
                region_name="us-east-1",
            )
    except (FileNotFoundError, KeyError):
        pass
    profile = os.environ.get("AWS_PROFILE", "AWSAdministratorAccess-073422073269")
    try:
        return boto3.Session(profile_name=profile, region_name="us-east-1")
    except Exception:
        return boto3.Session(region_name="us-east-1")


def get_secret_or_env(key: str, default: str = "") -> str:
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except (FileNotFoundError, KeyError):
        pass
    return os.environ.get(key, default)


aws_session = get_aws_session()
client = aws_session.client("bedrock-agentcore")
s3_client = aws_session.client("s3")
lambda_client = aws_session.client("lambda")

AGENT_ARN = get_secret_or_env(
    "AGENT_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:073422073269:runtime/riskradar_agent-gDKJmqByw0",
)
BUCKET_NAME = get_secret_or_env("S3_BUCKET_NAME", S3_BUCKET_NAME)
INGESTION_LAMBDA_NAME = get_secret_or_env("INGESTION_LAMBDA_NAME", DEFAULT_LAMBDA_NAME)
ADMIN_PASSWORD = get_secret_or_env("ADMIN_PASSWORD", "")


# --- S3 TICKER MANAGEMENT HELPERS ---
@st.cache_data(ttl=300, show_spinner=False)
def load_target_tickers_from_s3(bucket: str) -> tuple[list[str], str | None]:
    """Loads reference-data/target-tickers.json from S3."""
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=TARGET_TICKERS_KEY)
        raw = json.loads(resp["Body"].read().decode("utf-8"))
        if isinstance(raw, list):
            tickers = sorted({str(t).strip().upper() for t in raw if str(t).strip()})
            return tickers, None
        return [], "Invalid JSON format in S3 (expected a JSON array of ticker strings)."
    except Exception as e:
        return [], str(e)


@st.cache_data(ttl=3600, show_spinner=False)
def load_valid_sec_tickers(bucket: str) -> dict[str, str]:
    """Loads ticker -> company title mapping from S3 cache (reference-data/company_tickers.json) if available."""
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=CIK_MAPPING_KEY)
        data = json.loads(resp["Body"].read().decode("utf-8"))
        return {
            str(entry["ticker"]).upper(): entry.get("title", "")
            for entry in data.values()
            if "ticker" in entry
        }
    except Exception:
        return {}


def save_target_tickers_to_s3(bucket: str, tickers: list[str]) -> tuple[bool, str]:
    """Saves the cleaned, deduplicated, sorted ticker list back to S3."""
    cleaned = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=TARGET_TICKERS_KEY,
            Body=json.dumps(cleaned, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        load_target_tickers_from_s3.clear()
        return True, f"Saved {len(cleaned)} tickers to s3://{bucket}/{TARGET_TICKERS_KEY}"
    except Exception as e:
        return False, f"Failed to write to S3: {e}"


def trigger_ingestion_lambda(function_name: str, tickers: list[str] | None = None) -> tuple[bool, str]:
    """Asynchronously triggers the SEC ingestion Lambda function."""
    payload = {}
    if tickers:
        payload["tickers"] = tickers
    try:
        resp = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        status_code = resp.get("StatusCode", 0)
        target_desc = f"for {', '.join(tickers)}" if tickers else "for all target tickers"
        return True, f"Triggered Lambda `{function_name}` {target_desc} (HTTP {status_code})."
    except Exception as e:
        return False, f"Could not invoke Lambda `{function_name}`: {e}"


# --- SIDEBAR: MODEL & S3 TICKER MANAGER ---
with st.sidebar:
    st.header("About SecRadar")
    st.markdown(
        """
        This application backend is a serverless AI agent designed for financial analysts.
        It autonomously routes queries to an **Athena SQL Data Lake** for quantitative math, and an **Amazon Bedrock Knowledge Base** for qualitative SEC narratives.
        """
    )

    st.markdown("---")
    st.subheader("🧠 AI Model Selection")
    selected_model_label = st.selectbox(
        "Supervisor Model",
        options=list(MODEL_OPTIONS.keys()),
        index=0,
        help="Claude Sonnet 4.6 delivers deeper financial synthesis and multi-step SQL accuracy. Nova Pro and Haiku 4.5 provide lower-cost alternatives.",
    )
    selected_model_id = MODEL_OPTIONS[selected_model_label]

    st.markdown("---")
    st.subheader("🎯 Target Tickers (S3)")

    current_tickers, s3_err = load_target_tickers_from_s3(BUCKET_NAME)

    if s3_err:
        st.warning(f"Could not load tickers from S3 (`{BUCKET_NAME}`): {s3_err}")
    else:
        col_metric, col_refresh = st.columns([3, 1])
        with col_metric:
            st.caption(f"Tracking **{len(current_tickers)}** tickers in S3")
        with col_refresh:
            if st.button("🔄", help="Refresh ticker list from S3"):
                load_target_tickers_from_s3.clear()
                st.rerun()

        with st.expander(f"📋 View Active Tickers ({len(current_tickers)})", expanded=False):
            search_filter = st.text_input("Filter tickers", placeholder="e.g., AAPL, NVDA...", key="filter_ticker")
            filtered = (
                [t for t in current_tickers if search_filter.strip().upper() in t]
                if search_filter.strip()
                else current_tickers
            )
            if filtered:
                st.code(", ".join(filtered), language=None)
            else:
                st.caption("No matching tickers.")

        with st.expander("⚙️ Manage Target Tickers", expanded=False):
            entered_pw = st.text_input("🔐 Admin Password", type="password", key="admin_pw")
            if ADMIN_PASSWORD:
                is_unlocked = hmac.compare_digest(entered_pw, ADMIN_PASSWORD)
            else:
                entered_hash = hashlib.sha256(entered_pw.encode("utf-8")).hexdigest()
                is_unlocked = hmac.compare_digest(entered_hash, DEFAULT_ADMIN_PASSWORD_HASH)

            if entered_pw and not is_unlocked:
                st.error("Incorrect password.")
            elif not entered_pw:
                st.caption("Enter admin password to add or remove target tickers.")

            if is_unlocked:
                tab_add, tab_remove, tab_bulk = st.tabs(["➕ Add", "➖ Remove", "📝 JSON"])

                with tab_add:
                    raw_new = st.text_input(
                        "Add Ticker(s)",
                        placeholder="e.g., PLTR, UBER, SNOW",
                        help="Comma or space separated stock symbols.",
                    )
                    auto_ingest = st.checkbox(
                        "Trigger SEC ingestion Lambda for added tickers",
                        value=False,
                        help=f"Invokes Lambda `{INGESTION_LAMBDA_NAME}` for the new tickers right after saving.",
                    )
                    if st.button("Add to S3", type="primary", use_container_width=True):
                        parsed_new = [
                            t.strip().upper()
                            for t in re.split(r"[,\s]+", raw_new)
                            if t.strip()
                        ]
                        if not parsed_new:
                            st.warning("Please enter at least one ticker symbol.")
                        else:
                            sec_map = load_valid_sec_tickers(BUCKET_NAME)
                            invalid = [t for t in parsed_new if sec_map and t not in sec_map]
                            already_present = [t for t in parsed_new if t in current_tickers]
                            to_add = [t for t in parsed_new if t not in current_tickers and t not in invalid]

                            if invalid:
                                st.error(f"Unrecognized SEC ticker(s): {', '.join(invalid)}")
                            if already_present:
                                st.info(f"Already in target list: {', '.join(already_present)}")
                            if to_add:
                                updated_list = current_tickers + to_add
                                ok, msg = save_target_tickers_to_s3(BUCKET_NAME, updated_list)
                                if ok:
                                    st.success(f"Added {', '.join(to_add)} ({len(updated_list)} total).")
                                    if auto_ingest:
                                        ing_ok, ing_msg = trigger_ingestion_lambda(INGESTION_LAMBDA_NAME, to_add)
                                        if ing_ok:
                                            st.info(ing_msg)
                                        else:
                                            st.warning(ing_msg)
                                    time.sleep(0.8)
                                    st.rerun()
                                else:
                                    st.error(msg)

                with tab_remove:
                    to_remove = st.multiselect(
                        "Select ticker(s) to remove",
                        options=current_tickers,
                    )
                    if st.button("Remove Selected", use_container_width=True, disabled=not to_remove):
                        remaining = [t for t in current_tickers if t not in set(to_remove)]
                        ok, msg = save_target_tickers_to_s3(BUCKET_NAME, remaining)
                        if ok:
                            st.success(f"Removed {', '.join(to_remove)}.")
                            time.sleep(0.8)
                            st.rerun()
                        else:
                            st.error(msg)

                with tab_bulk:
                    edited_json = st.text_area(
                        "Edit raw JSON list",
                        value=json.dumps(current_tickers, indent=2),
                        height=200,
                    )
                    if st.button("Save JSON to S3", use_container_width=True):
                        try:
                            parsed_list = json.loads(edited_json)
                            if not isinstance(parsed_list, list):
                                st.error("JSON must be a list of ticker strings, e.g. [\"AAPL\", \"MSFT\"].")
                            else:
                                ok, msg = save_target_tickers_to_s3(BUCKET_NAME, parsed_list)
                                if ok:
                                    st.success(msg)
                                    time.sleep(0.8)
                                    st.rerun()
                                else:
                                    st.error(msg)
                        except json.JSONDecodeError as e:
                            st.error(f"Invalid JSON: {e}")

# Fail loudly if the ARN wasn't found in either place
if not AGENT_ARN:
    st.error("CRITICAL: AGENT_ARN is not set. Please configure your environment variables or Streamlit secrets.")
    st.stop()

# --- CHAT HISTORY ---
if "messages" not in st.session_state:
    st.session_state.messages = []

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# --- SECURITY GUARDRAILS ---
MAX_REQUESTS_PER_SESSION = 5  # Max questions a single person can ask
COOLDOWN_SECONDS = 60         # Seconds they must wait between questions

if "request_count" not in st.session_state:
    st.session_state.request_count = 0
if "last_request_time" not in st.session_state:
    st.session_state.last_request_time = 0

# --- USER INPUT & API CALL ---
if prompt := st.chat_input(
    "E.g., What was Tesla's revenue in 2025, and based on their recent filings, what are their primary supply chain risks?",
    max_chars=300,
):
    current_time = time.time()
    time_since_last = current_time - st.session_state.last_request_time

    # Guardrail 1: Hard Cap
    if st.session_state.request_count >= MAX_REQUESTS_PER_SESSION:
        st.error(
            f"You have reached the maximum number of queries ({MAX_REQUESTS_PER_SESSION}) for this demo. Thank you for testing SecRadar!"
        )
        st.stop()

    # Guardrail 2: Rate Limiting (Cooldown)
    if time_since_last < COOLDOWN_SECONDS:
        st.warning(
            f"Please wait {int(COOLDOWN_SECONDS - time_since_last)} more seconds before asking another question to prevent spam."
        )
        st.stop()

    # Record the new request
    st.session_state.request_count += 1
    st.session_state.last_request_time = current_time

    # 1. Display the user's prompt
    st.chat_message("user").markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    # 2. Call the AgentCore Cloud Endpoint
    with st.chat_message("assistant"):
        with st.spinner(f"SecRadar ({selected_model_label.split(' (')[0]}) is analyzing SEC filings..."):
            try:
                chat_history = st.session_state.messages[:-1]

                payload_data = json.dumps(
                    {
                        "prompt": prompt,
                        "history": chat_history,
                        "model_id": selected_model_id,
                    }
                ).encode("utf-8")

                boto_response = client.invoke_agent_runtime(
                    agentRuntimeArn=AGENT_ARN,
                    runtimeSessionId=st.session_state.session_id,
                    payload=payload_data,
                )

                if "response" in boto_response:
                    raw_body = boto_response["response"].read().decode("utf-8")
                    try:
                        parsed_json = json.loads(raw_body)
                        final_answer = parsed_json.get("result", raw_body)
                    except json.JSONDecodeError:
                        final_answer = raw_body
                else:
                    final_answer = json.dumps(boto_response, default=str)

                # Escape bare dollar signs so Streamlit does not treat financial numbers as LaTeX math
                final_answer = final_answer.replace("$", r"\$")

                st.markdown(final_answer)
                st.session_state.messages.append({"role": "assistant", "content": final_answer})

            except Exception as e:
                error_string = str(e)

                if "AccessDenied" in error_string or "403" in error_string:
                    st.error(
                        "🔒 **Access Denied:** The AI agent is currently unavailable due to security restrictions. If you are the administrator, please check the IAM policy permissions."
                    )
                elif "ThrottlingException" in error_string or "429" in error_string:
                    st.error(
                        "🚦 **Traffic Heavy:** The SEC agent is currently experiencing high traffic. Please try again in a few moments."
                    )
                else:
                    st.error(
                        "⚠️ **System Error:** An unexpected error occurred while analyzing the SEC filings. Please try again later."
                    )

                print(f"Backend AWS Error: {error_string}", flush=True)
