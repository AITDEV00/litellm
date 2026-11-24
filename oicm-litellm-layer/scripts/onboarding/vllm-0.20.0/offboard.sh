#!/bin/bash
# =============================================================================
# MLOps Model Server Offboarding Script
# =============================================================================
# This script removes/deletes an existing model server from the OICM platform.
#
# Usage:
#   ./offboard.sh                     # Interactive mode - lists servers and prompts for selection
#   ./offboard.sh --id <server-id>    # Delete by ID directly
#   ./offboard.sh --list              # List all model servers without deleting
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

# =============================================================================
# Parse Arguments
# =============================================================================
LIST_ONLY=false
SERVER_ID=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --id)
            SERVER_ID="$2"
            shift 2
            ;;
        --list|-l)
            LIST_ONLY=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --id <server-id>   Delete model server by ID directly"
            echo "  --list, -l         List all model servers without deleting"
            echo "  --help, -h         Show this help message"
            echo ""
            echo "If no options are provided, the script runs in interactive mode."
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information."
            exit 1
            ;;
    esac
done

# =============================================================================
# Functions
# =============================================================================

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
    echo ""
}

fetch_model_servers() {
    local url="${OICM_HOST}/api/v1/model_servers/summary"

    local response
    response=$(curl -s -k -X GET "$url" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "Content-Type: application/json")

    # Check if response is valid JSON array
    if ! echo "$response" | jq -e '. | type == "array"' > /dev/null 2>&1; then
        # Try to extract from common wrapper keys
        for key in items model_servers servers data; do
            if echo "$response" | jq -e ".$key | type == \"array\"" > /dev/null 2>&1; then
                response=$(echo "$response" | jq ".$key")
                break
            fi
        done
    fi

    echo "$response"
}

list_model_servers() {
    local servers="$1"

    local count
    count=$(echo "$servers" | jq 'length')

    if [[ "$count" -eq 0 ]]; then
        echo "No model servers found."
        return 1
    fi

    echo "=== Model Servers ($count total) ==="
    printf "%-4s %-36s %-25s %-15s %-10s\n" "#" "ID" "Name" "Version" "Enabled"
    printf "%s\n" "$(printf '=%.0s' {1..95})"

    local i=0
    while IFS= read -r server; do
        local id name tag enabled
        id=$(echo "$server" | jq -r '.id // "N/A"')
        name=$(echo "$server" | jq -r '.name // "N/A"')
        tag=$(echo "$server" | jq -r '.tag // "N/A"')
        enabled=$(echo "$server" | jq -r '.enabled // false')

        printf "%-4s %-36s %-25s %-15s %-10s\n" "$((i+1))" "$id" "$name" "$tag" "$enabled"
        i=$((i+1))
    done <<EOF
$(echo "$servers" | jq -c '.[]')
EOF

    echo ""
    return 0
}

select_model_server() {
    local servers="$1"

    local count
    count=$(echo "$servers" | jq 'length')

    echo "Enter the number of the model server to delete (1-$count), or 'q' to quit:" >&2
    read -p "> " selection

    if [[ "$selection" == "q" ]] || [[ "$selection" == "Q" ]]; then
        echo "Operation cancelled." >&2
        exit 0
    fi

    # Validate input is a number
    if ! [[ "$selection" =~ ^[0-9]+$ ]]; then
        echo "Error: Invalid input. Please enter a number." >&2
        exit 1
    fi

    # Validate range
    if [[ "$selection" -lt 1 ]] || [[ "$selection" -gt "$count" ]]; then
        echo "Error: Selection out of range. Please enter a number between 1 and $count." >&2
        exit 1
    fi

    # Get the selected server (0-indexed)
    local index=$((selection - 1))
    local selected_server
    selected_server=$(echo "$servers" | jq ".[$index]")

    echo "$selected_server"
}

delete_model_server() {
    local server_id="$1"
    local server_name="$2"

    echo "Deleting model server..."
    echo "  ID:   $server_id"
    echo "  Name: $server_name"
    echo ""

    local url="${OICM_HOST}/api/v1/model_servers/${server_id}"

    local response
    local http_code

    # Make the request and capture both response body and HTTP code
    response=$(curl -s -k -w "\n%{http_code}" -X DELETE "$url" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "Content-Type: application/json")

    # Extract HTTP code from response
    http_code=$(echo "$response" | tail -n1)
    response_body=$(echo "$response" | sed '$d')

    if [[ "$http_code" -ge 200 ]] && [[ "$http_code" -lt 300 ]]; then
        echo "=== SUCCESS ==="
        echo "Model Server '$server_name' ($server_id) deleted successfully!"
        echo "HTTP Status: $http_code"
        echo ""
    else
        echo "=== ERROR ==="
        echo "Failed to delete model server"
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
for cmd in curl jq; do
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
)

for var in "${required_vars[@]}"; do
    if [[ -z "${!var}" ]]; then
        echo "Error: Required environment variable $var is not set in .env file"
        exit 1
    fi
done

# Authenticate
authenticate

# Fetch model servers
echo "Fetching model servers from $OICM_HOST..."
SERVERS=$(fetch_model_servers)

# Check if we got valid data
if ! echo "$SERVERS" | jq -e '. | type == "array"' > /dev/null 2>&1; then
    echo "Error: Failed to fetch model servers or invalid response"
    echo "Response: $SERVERS"
    exit 1
fi

SERVER_COUNT=$(echo "$SERVERS" | jq 'length')

if [[ "$SERVER_COUNT" -eq 0 ]]; then
    echo "No model servers found. Nothing to delete."
    exit 0
fi

# List mode only
if [[ "$LIST_ONLY" == true ]]; then
    list_model_servers "$SERVERS"
    exit 0
fi

# If server ID provided directly
if [[ -n "$SERVER_ID" ]]; then
    # Find server by ID
    SELECTED_SERVER=$(echo "$SERVERS" | jq --arg id "$SERVER_ID" '.[] | select(.id == $id)')

    if [[ -z "$SELECTED_SERVER" ]] || [[ "$SELECTED_SERVER" == "null" ]]; then
        echo "Error: Model server with ID '$SERVER_ID' not found."
        echo ""
        echo "Available model servers:"
        list_model_servers "$SERVERS"
        exit 1
    fi

    SELECTED_ID="$SERVER_ID"
    SELECTED_NAME=$(echo "$SELECTED_SERVER" | jq -r '.name // "Unknown"')
else
    # Interactive mode - list and select
    list_model_servers "$SERVERS"

    SELECTED_SERVER=$(select_model_server "$SERVERS")
    SELECTED_ID=$(echo "$SELECTED_SERVER" | jq -r '.id')
    SELECTED_NAME=$(echo "$SELECTED_SERVER" | jq -r '.name // "Unknown"')
fi

# Confirm deletion
echo ""
echo "You are about to delete:"
echo "  Name:    $SELECTED_NAME"
echo "  ID:      $SELECTED_ID"
echo "  Version: $(echo "$SELECTED_SERVER" | jq -r '.tag // "N/A"')"
echo ""
read -p "Are you sure you want to delete this model server? (y/n): " confirm

if [[ "$confirm" != "y" ]] && [[ "$confirm" != "Y" ]]; then
    echo "Deletion cancelled."
    exit 0
fi

# Delete the model server
delete_model_server "$SELECTED_ID" "$SELECTED_NAME"
