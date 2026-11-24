#!/bin/bash
# =============================================================================
# MLOps Model Server Onboarding Script
# =============================================================================
# This script onboards a single model server to the OICM platform.
#
# Usage:
#   1. Configure the .env file with your credentials and settings
#   2. Run: ./onboard.sh
#   3. For dry-run (see payload without sending): ./onboard.sh --dry-run
# =============================================================================

set -e

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load environment variables from .env file
ENV_FILE="${SCRIPT_DIR}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: .env file not found at $ENV_FILE"
    exit 1
fi

# Source the .env file
set -a
source "$ENV_FILE"
set +a

# Check for dry-run flag
DRY_RUN=false
if [[ "$1" == "--dry-run" ]] || [[ "$1" == "-d" ]]; then
    DRY_RUN=true
    echo "=== DRY RUN MODE - No requests will be sent ==="
fi

# =============================================================================
# Functions
# =============================================================================

# Parse "device1=image1,device2=image2" into a JSON object.
# Normalizes nvidia variants (nvidia-hopper, nvidia-turing) to "nvidia".
parse_devices_to_json() {
    local devices_str="$1"
    local result="{}"
    local pair key val
    local IFS=","
    for pair in $devices_str; do
        key="${pair%%=*}"
        val="${pair#*=}"
        case "$key" in
            nvidia|nvidia-hopper|nvidia-turing)
                key="nvidia"
                ;;
        esac
        result=$(echo "$result" | jq --arg k "$key" --arg v "$val" '. + {($k): $v}')
    done
    echo "$result"
}

# =============================================================================
# Configuration Resolution
# =============================================================================

if [[ -z "$DEVICES" ]]; then
    echo "Error: DEVICES is not set in .env file."
    exit 1
fi

FINAL_DEVICES_JSON=$(parse_devices_to_json "$DEVICES")
FINAL_DISPLAY_NAME="$MODEL_SERVER_NAME"

print_config() {
    echo ""
    echo "=== Model Server Configuration ==="
    echo "Display Name:    $FINAL_DISPLAY_NAME"
    echo "Tag/Version:     $MODEL_SERVER_TAG"
    echo "Family:          $MODEL_SERVER_FAMILY"
    echo "Task Types:      $TASK_TYPES"
    echo "Health Check:    $HEALTH_CHECK_ENDPOINT"
    echo "Enable Metrics:  $ENABLE_METRICS"
    echo "Multi-node:      $MULTINODE_INFERENCE_SUPPORT"
    echo "OICM Host:       $OICM_HOST"
    echo "Devices:"
    echo "$FINAL_DEVICES_JSON" | jq -r 'to_entries[] | "  \(.key): \(.value)"'
    echo "=================================="
    echo ""
}

authenticate() {
    echo "Authenticating with $AUTH_BASE_URL..."

    local auth_url="${AUTH_BASE_URL}/realms/admin/protocol/openid-connect/token"
    local auth_payload="client_id=admin&username=${APPLICATION_ADMIN_USERNAME}&password=${APPLICATION_ADMIN_PASSWORD}&grant_type=password&scope=openid"

    local response
    response=$(curl -s -k -X POST "$auth_url" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "$auth_payload")

    ACCESS_TOKEN=$(echo "$response" | jq -r '.access_token')

    if [[ "$ACCESS_TOKEN" == "null" ]] || [[ -z "$ACCESS_TOKEN" ]]; then
        echo "Error: Authentication failed"
        echo "Response: $response"
        exit 1
    fi

    echo "Authentication successful."
}

read_json_file() {
    local json_path="$1"

    # If path is empty or not set, return empty JSON
    if [[ -z "$json_path" ]]; then
        echo "{}"
        return
    fi

    # Check if path is relative or absolute
    if [[ "$json_path" != /* ]]; then
        json_path="${SCRIPT_DIR}/${json_path}"
    fi

    if [[ ! -f "$json_path" ]]; then
        # Output warning to stderr so it doesn't interfere with JSON output
        echo "Note: JSON file not found at $json_path. Using empty arguments." >&2
        echo "{}"
        return
    fi

    cat "$json_path"
}

build_payload() {
    local json_data
    json_data=$(read_json_file "$MODEL_SERVER_ARGS_JSON")

    # Extract components from JSON file
    local server_arguments
    local inference_arguments
    local tasks_base_url
    local lora_config
    local lora_modules_separator
    local supported_auto_scaling_metrics
    local supported_models_architectures

    server_arguments=$(echo "$json_data" | jq -c '.server_arguments // []')
    inference_arguments=$(echo "$json_data" | jq -c '.inference_arguments // []')
    tasks_base_url=$(echo "$json_data" | jq -c '.tasks_base_url // []')
    lora_config=$(echo "$json_data" | jq -r '.lora_config // empty')
    lora_modules_separator=$(echo "$json_data" | jq -r '.lora_modules_separator // empty')
    supported_auto_scaling_metrics=$(echo "$json_data" | jq -c '.supported_auto_scaling_metrics // []')
    supported_models_architectures=$(echo "$json_data" | jq -c '.supported_models_architectures // []')

    # Convert task types from comma-separated to JSON array
    local task_types_array
    task_types_array=$(echo "$TASK_TYPES" | tr ',' '\n' | jq -R . | jq -s .)

    # Convert LoRA tasks from comma-separated to JSON array
    local lora_tasks_array="[]"
    if [[ -n "$LORA_TASKS" ]]; then
        lora_tasks_array=$(echo "$LORA_TASKS" | tr ',' '\n' | jq -R . | jq -s .)
    fi

    # Convert enable_metrics to boolean
    local enable_metrics_bool="true"
    if [[ "$ENABLE_METRICS" == "false" ]] || [[ "$ENABLE_METRICS" == "0" ]]; then
        enable_metrics_bool="false"
    fi

    # Convert multinode_inference_support to boolean (default false)
    local multinode_inference_support_bool="false"
    if [[ "$MULTINODE_INFERENCE_SUPPORT" == "true" ]] || [[ "$MULTINODE_INFERENCE_SUPPORT" == "1" ]]; then
        multinode_inference_support_bool="true"
    fi

    # Generate UUID for the model server
    local generated_id
    generated_id=$(uuidgen | tr '[:upper:]' '[:lower:]')

    # Build the payload
    local payload
    payload=$(jq -n \
        --arg id "$generated_id" \
        --arg name "$FINAL_DISPLAY_NAME" \
        --arg tag "$MODEL_SERVER_TAG" \
        --arg family "$MODEL_SERVER_FAMILY" \
        --argjson devices "$FINAL_DEVICES_JSON" \
        --argjson task_types "$task_types_array" \
        --arg startup_command "$STARTUP_COMMAND" \
        --argjson server_arguments "$server_arguments" \
        --argjson inference_arguments "$inference_arguments" \
        --argjson tasks_base_url "$tasks_base_url" \
        --arg health_check "$HEALTH_CHECK_ENDPOINT" \
        --argjson enable_metrics "$enable_metrics_bool" \
        --argjson supported_auto_scaling_metrics "$supported_auto_scaling_metrics" \
        --arg lora_config "$lora_config" \
        --arg lora_modules_separator "$lora_modules_separator" \
        --argjson lora_tasks "$lora_tasks_array" \
        --argjson supported_models_architectures "$supported_models_architectures" \
        --argjson multinode_inference_support "$multinode_inference_support_bool" \
        '{
            "_id": $id,
            "id": $id,
            "name": $name,
            "tag": $tag,
            "family": (if $family == "" then null else $family end),
            "devices": $devices,
            "task_types": $task_types,
            "startup_command": (if $startup_command == "" then null else $startup_command end),
            "server_arguments": $server_arguments,
            "inference_arguments": $inference_arguments,
            "tasks_base_url": $tasks_base_url,
            "enabled": true,
            "health_check_endpoint": $health_check,
            "enable_metrics": $enable_metrics,
            "supported_auto_scaling_metrics": $supported_auto_scaling_metrics,
            "lora_config": (if $lora_config == "" then null else $lora_config end),
            "lora_modules_separator": (if $lora_modules_separator == "" then null else $lora_modules_separator end),
            "supported_tasks_with_lora_adapters": $lora_tasks,
            "supported_models_architectures": $supported_models_architectures,
            "multinode_inference_support": $multinode_inference_support
        }')

    echo "$payload"
}

create_model_server() {
    local payload="$1"

    echo "Creating model server..."

    local url="${OICM_HOST}/api/v1/model_servers"

    local response
    local http_code

    # Make the request and capture both response body and HTTP code
    response=$(curl -s -k -w "\n%{http_code}" -X POST "$url" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$payload")

    # Extract HTTP code from response
    http_code=$(echo "$response" | tail -n1)
    response_body=$(echo "$response" | sed '$d')

    if [[ "$http_code" -ge 200 ]] && [[ "$http_code" -lt 300 ]]; then
        echo ""
        echo "=== SUCCESS ==="
        echo "Model Server '$FINAL_DISPLAY_NAME' onboarded successfully!"
        echo "HTTP Status: $http_code"
        echo ""
    else
        echo ""
        echo "=== ERROR ==="
        echo "Failed to create model server"
        echo "HTTP Status: $http_code"
        echo "Response: $response_body"
        echo ""
        exit 1
    fi
}

# =============================================================================
# Main Execution
# =============================================================================

# Check required tools
for cmd in curl jq uuidgen; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "Error: Required command '$cmd' is not installed."
        exit 1
    fi
done

# Validate required environment variables
required_vars=(
    "OICM_HOST"
    "AUTH_BASE_URL"
    "APPLICATION_ADMIN_USERNAME"
    "APPLICATION_ADMIN_PASSWORD"
    "MODEL_SERVER_NAME"
    "MODEL_SERVER_TAG"
    "TASK_TYPES"
    "HEALTH_CHECK_ENDPOINT"
)

for var in "${required_vars[@]}"; do
    if [[ -z "${!var}" ]]; then
        echo "Error: Required environment variable $var is not set in .env file"
        exit 1
    fi
done

# Print configuration
print_config

# Build payload
PAYLOAD=$(build_payload)

if [[ "$DRY_RUN" == true ]]; then
    echo "=== Generated Payload (Dry Run) ==="
    echo "$PAYLOAD" | jq .
    echo ""
    echo "No request sent (dry-run mode)."
    exit 0
fi

# Confirm before proceeding
read -p "Do you want to proceed with onboarding? (y/n): " confirm
if [[ "$confirm" != "y" ]] && [[ "$confirm" != "Y" ]]; then
    echo "Onboarding cancelled."
    exit 0
fi

# Authenticate
authenticate

# Create model server
create_model_server "$PAYLOAD"
