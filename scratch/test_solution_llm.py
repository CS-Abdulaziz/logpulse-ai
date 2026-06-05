import os
import sys
from dotenv import load_dotenv

# Load paths
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, _PROJECT_ROOT)

load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"))

from ai_core.workflow.state import LogState, DiagnosticResult, RagResult, ClassificationData, SeverityLevel
from ai_core.workflow.agents.solution_agent import _build_prompt, GEMINI_AVAILABLE, _genai_client
import google.genai.types as genai_types

# Set up state
state = LogState(
    raw_log="OOMKilled: pod/worker-deployment-5f7c exceeded 256Mi",
    classification=ClassificationData(
        category="Memory",
        source="Kubernetes",
        severity=SeverityLevel.CRITICAL,
        summary="Container OOMKilled — exceeded 256Mi memory limit.",
    ),
    rag_result=RagResult(
        playbook_steps=(
            "[Playbook: Kubernetes OOMKilled — Memory Limit Exceeded]\n"
            "Severity: Critical\n\n"
            "Resolution steps:\n"
            "  • Check current memory requests/limits with kubectl describe pod\n"
            "  • Increase memory limit in the Deployment spec\n"
            "  • kubectl patch deployment worker-deployment -p '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"worker\",\"resources\":{\"limits\":{\"memory\":\"512Mi\"}}}]}}}}'\n"
        ),
        rag_confidence=0.7641,
    ),
    diagnostic_result=DiagnosticResult(
        root_cause="The pod's configured memory limit of 256Mi was insufficient for its operational memory requirements, causing Kubernetes to OOMKill it.",
        confidence=0.95,
        reasoning="OOMKilled log + Memory playbook.",
        used_history=False,
        used_playbook=True,
        source="llm",
    )
)

prompt = _build_prompt(state)

if not GEMINI_AVAILABLE or _genai_client is None:
    print("Gemini client not initialized!")
    sys.exit(1)

# Modify configuration to allow more tokens
config = genai_types.GenerateContentConfig(
    temperature=0.3,
    max_output_tokens=2048,
)

print("Calling Gemini model with max_output_tokens=2048...")
try:
    response = _genai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=config,
    )
    print("===== RESPONSE METADATA =====")
    print(f"Candidates count: {len(response.candidates)}")
    candidate = response.candidates[0]
    print(f"Finish reason: {candidate.finish_reason}")
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        print(f"Prompt tokens: {response.usage_metadata.prompt_token_count}")
        print(f"Candidates tokens: {response.usage_metadata.candidates_token_count}")
        print(f"Total tokens: {response.usage_metadata.total_token_count}")
    else:
        print("No usage metadata available.")
    print("===== RAW RESPONSE =====")
    sys.stdout.buffer.write(response.text.encode('utf-8'))
    print("\n========================\n")
except Exception as e:
    print(f"Error calling Gemini: {e}")
