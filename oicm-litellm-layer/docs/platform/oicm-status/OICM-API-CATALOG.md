# OICM Status REST API - live catalog

- Base URL: `https://oicm.adeoaiengine.ecouncil.ae`
- Auth: realm `adeo`, client `adeo`, password grant (`svc-litellm-controller`)
- Workspace: `dfec2a9f-cc5c-4b7b-b608-990d3804e80c`
- Captured: 2026-09-29 (dev controller, live responses)

Every endpoint below was probed live. Schemas are derived from the captured payload, not guessed.

---

## List deployments (workspace)

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/deployments`
All model deployments in the workspace.
**HTTP 200**

**Response (truncated):**

```json
{
  "items": [
    {
      "_created_at": "2026-09-18T12:02:43.031000Z",
      "_deleted_at": null,
      "_deleted_id": null,
      "_lifecycle_stage": "active",
      "_tenant_id": "adeo",
      "_updated_at": "2026-09-18T14:06:27.314000Z",
      "_version": 6,
      "auto_scaling_options": null,
      "can_stop_deployment": true,
      "custom_docker_image": null,
      "custom_model_server": null,
      "data_volume_id": null,
      "data_volume_model_path": null,
      "deployment_template_id": null,
      "deployment_type": "Model Registry",
      "enable_auto_scaling": false,
      "env_vars": {},
      "error_msg": null,
      "external_sources": null,
      "extra_args": null,
      "id": "4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8",
      "inference_task": "Image-Text-to-Text",
      "is_idle": false,
      "is_unscalable": false,
      "lora_adapters": [],
      "model_id": "18657973-661a-4579-9f39-9f95be9a85c4",
      "model_name": "moonshotai/Kimi-K3",
      "model_server": {
        "args": null,
        "developer_args": "--trust-remote-code",
        "developer_mode": true,
        "model_server_id": "8af62574-3fc8-4649-8938-1fa61629be57",
        "name": "SGLang",
        "tag": "0.5.19-cu129-amd64"
      },
      "model_server_snapshot": {
        "_created_at": "2026-09-08T07:48:12.974000Z",
        "_deleted_at": null,
        "_deleted_id": null,
        "_lifecycle_stage": "active",
        "_tenant_id": "admin",
        "_updated_at": "2026-09-08T07:48:12.971000Z",
        "_version": 1,
        "devices": {
          "nvidia": "openinnovationai/platform/mlops/mlops-serving/sglang:0.5.19-cu129-amd64"
        },
        "enable_metrics": true,
        "enabled": true,
        "family": "SGLang",
        "health_check_endpoint": "/health",
        "id": "8af62574-3fc8-4649-8938-1fa61629be57",
        "inference_arguments": [
          {
            "arguments": [
              {
                "default": 0.7,
                "desc": "What sampling temperature to use, between 0 and 2. Higher values like 0.8 will m\u2026",
                "format": "float",
                "label": "temperature",
                "max": 2.0,
                "min": 0.0,
                "name": "temperature",
                "options": null,
                "required": false,
                "type": "number"
              },
              {
                "default": 0.9,
                "desc": "An alternative to sampling with temperature, called nucleus sampli
```

**Field schema:**

```json
{
  "items": [
    {
      "_created_at": "str",
      "_deleted_at": "NoneType",
      "_deleted_id": "NoneType",
      "_lifecycle_stage": "str",
      "_tenant_id": "str",
      "_updated_at": "str",
      "_version": "int",
      "auto_scaling_options": "NoneType",
      "can_stop_deployment": "bool",
      "custom_docker_image": "NoneType",
      "custom_model_server": "NoneType",
      "data_volume_id": "NoneType",
      "data_volume_model_path": "NoneType",
      "deployment_template_id": "NoneType",
      "deployment_type": "str",
      "enable_auto_scaling": "bool",
      "env_vars": {},
      "error_msg": "NoneType",
      "external_sources": "NoneType",
      "extra_args": "NoneType",
      "id": "str",
      "inference_task": "str",
      "is_idle": "bool",
      "is_unscalable": "bool",
      "lora_adapters": "[]",
      "model_id": "str",
      "model_name": "str",
      "model_server": {
        "args": "...",
        "developer_args": "...",
        "developer_mode": "...",
        "model_server_id": "...",
        "name": "...",
        "tag": "..."
      },
      "model_server_snapshot": {
        "_created_at": "...",
        "_deleted_at": "...",
        "_deleted_id": "...",
        "_lifecycle_stage": "...",
        "_tenant_id": "...",
        "_updated_at": "...",
        "_version": "...",
        "devices": "...",
        "enable_metrics": "...",
        "enabled": "...",
        "family": "...",
        "health_check_endpoint": "...",
        "id": "...",
        "inference_arguments": "...",
        "lora_config": "...",
        "lora_modules_separator": "...",
        "multinode_inference_support": "...",
        "name": "...",
        "server_arguments": "...",
        "startup_command": "...",
        "supported_auto_scaling_metrics": "...
```

---

## Get deployment

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/deployments/4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8`
Single deployment record. id == workload_id.
**HTTP 200**

**Response (truncated):**

```json
{
  "id": "4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8",
  "workspace_id": "dfec2a9f-cc5c-4b7b-b608-990d3804e80c",
  "name": "ANOTHER: moonshotai/Kimi-K3",
  "deployment_type": "Model Registry",
  "model_version_id": null,
  "registered_model_id": null,
  "model_id": "18657973-661a-4579-9f39-9f95be9a85c4",
  "custom_docker_image": null,
  "deployment_template_id": null,
  "model_server": {
    "model_server_id": "8af62574-3fc8-4649-8938-1fa61629be57",
    "name": "SGLang",
    "tag": "0.5.19-cu129-amd64",
    "args": null,
    "developer_mode": true,
    "developer_args": "--trust-remote-code"
  },
  "enable_auto_scaling": false,
  "auto_scaling_options": null,
  "replicas": 1,
  "inference_task": "Image-Text-to-Text",
  "resources": {
    "use_gpu": true,
    "accelerator": "B300",
    "accelerator_count": 8,
    "cpu": 32,
    "memory": 1512,
    "storage": null,
    "accelerator_slice": null,
    "number_of_nodes": null
  },
  "status": "Ready",
  "error_msg": null,
  "inference_url": "https://inference.adeoaiengine.ecouncil.ae/models/4f0a7c56-2ce3-4968-8022-c1d80f\u2026",
  "internal_inference_url": "http://api-gateway-service.mlops.svc.cluster.local:8080/models/4f0a7c56-2ce3-496\u2026",
  "env_vars": {},
  "extra_args": null,
  "use_lora_adapters": false,
  "lora_adapters": [],
  "model_server_snapshot": {
    "id": "8af62574-3fc8-4649-8938-1fa61629be57",
    "name": "SGLang",
    "tag": "0.5.19-cu129-amd64",
    "family": "SGLang",
    "devices": {
      "nvidia": "openinnovationai/platform/mlops/mlops-serving/sglang:0.5.19-cu129-amd64"
    },
    "task_types": [
      "Text Generation",
      "Image-Text-to-Text",
      "Text-to-Image"
    ],
    "health_check_endpoint": "/health"
  },
  "custom_model_server": null,
  "external_sources": null
}
```

**Field schema:**

```json
{
  "id": "str",
  "workspace_id": "str",
  "name": "str",
  "deployment_type": "str",
  "model_version_id": "NoneType",
  "registered_model_id": "NoneType",
  "model_id": "str",
  "custom_docker_image": "NoneType",
  "deployment_template_id": "NoneType",
  "model_server": {
    "model_server_id": "str",
    "name": "str",
    "tag": "str",
    "args": "NoneType",
    "developer_mode": "bool",
    "developer_args": "str"
  },
  "enable_auto_scaling": "bool",
  "auto_scaling_options": "NoneType",
  "replicas": "int",
  "inference_task": "str",
  "resources": {
    "use_gpu": "bool",
    "accelerator": "str",
    "accelerator_count": "int",
    "cpu": "int",
    "memory": "int",
    "storage": "NoneType",
    "accelerator_slice": "NoneType",
    "number_of_nodes": "NoneType"
  },
  "status": "str",
  "error_msg": "NoneType",
  "inference_url": "str",
  "internal_inference_url": "str",
  "env_vars": {},
  "extra_args": "NoneType",
  "use_lora_adapters": "bool",
  "lora_adapters": "[]",
  "model_server_snapshot": {
    "id": "str",
    "name": "str",
    "tag": "str",
    "family": "str",
    "devices": {
      "nvidia": "str"
    },
    "task_types": [
      "str",
      "... (3 items)"
    ],
    "health_check_endpoint": "str"
  },
  "custom_model_server": "NoneType",
  "external_sources": "NoneType"
}
```

---

## Deployment health

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/deployments/4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8/health`
Independent readiness signal (is_ready).
**HTTP 200**

**Response (truncated):**

```json
{
  "is_health_check_supported": true,
  "is_ready": false,
  "message": "Deployment is not ready"
}
```

**Field schema:**

```json
{
  "is_health_check_supported": "bool",
  "is_ready": "bool",
  "message": "str"
}
```

---

## Deployment summary (workspace)

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/deployment_summary`
Roll-up of deployment states.
**HTTP 200**

**Response (truncated):**

```json
{
  "items": [
    {
      "_created_at": "2026-09-23T13:24:59.523000Z",
      "_updated_at": "2026-09-23T15:38:44.331000Z",
      "can_stop_deployment": true,
      "custom_model_server": null,
      "data_volume_id": null,
      "data_volume_lifecycle_stage": null,
      "data_volume_name": null,
      "deployment_id": "b3784f33-e53f-4dfb-b8ae-4f588f44f926",
      "deployment_name": "Qwen/Qwen3.8-Flash-Next-FP8",
      "deployment_template": null,
      "deployment_type": "Model Registry",
      "developer_args": "--trust-remote-code",
      "developer_mode": true,
      "docker_image_name": null,
      "env_vars": {},
      "error_msg": null,
      "inference_task": "Image-Text-to-Text",
      "instances": [
        {
          "kind": "Pod",
          "metadata": {
            "ready": true
          },
          "name": "j-b3784f33-e53f-4dfb-b8ae-4f588f44f926-5f498f45f-rm66h",
          "node": "adeo-gpu-02",
          "status": "Running",
          "status_msg": ""
        }
      ],
      "lora_adapters": null,
      "model_name": "Qwen/Qwen3.8-Flash-Next-FP8",
      "model_server_id": "8af62574-3fc8-4649-8938-1fa61629be57",
      "model_server_name": "SGLang",
      "model_server_tag": "0.5.19-cu129-amd64",
      "model_version_id": null,
      "model_version_name": null,
      "model_version_version": null,
      "registered_model_id": null,
      "registered_model_name": null,
      "replicas": 1,
      "resources": {
        "accelerator": "H200",
        "accelerator_count": 2,
        "accelerator_slice": null,
        "cpu": 16,
        "memory": 512,
        "number_of_nodes": null,
        "storage": null,
        "use_gpu": true
      },
      "status": "Ready",
      "status_detail": [
        {
          "kind": "Deployment",
          "metadata": {
            "available": true,
            "available_replicas": 1,
            "progressing": true
          },
          "name": "j-b3784f33-e53f-4dfb-b8ae-4f588f44f926",
          "node": "",
          "status": "Ready",
          "status_msg": "1/1 replicas ready"
        },
        {
          "kind": "Pod",
          "metadata": {
            "ready": true
          },
          "name": "j-b3784f33-e53f-4dfb-b8ae-4f588f44f926-5f498f45f-rm66h",
          "node": "adeo-gpu-02",
          "status": "Running",
          "status_msg": ""
        }
      ],
      "supported_auto_scaling_metrics": [
        "ml_model_concurrent_requests",
        "num_of_requests_waiting_in_queue"
      ],
```

**Field schema:**

```json
{
  "items": [
    {
      "_created_at": "str",
      "_updated_at": "str",
      "can_stop_deployment": "bool",
      "custom_model_server": "NoneType",
      "data_volume_id": "NoneType",
      "data_volume_lifecycle_stage": "NoneType",
      "data_volume_name": "NoneType",
      "deployment_id": "str",
      "deployment_name": "str",
      "deployment_template": "NoneType",
      "deployment_type": "str",
      "developer_args": "str",
      "developer_mode": "bool",
      "docker_image_name": "NoneType",
      "env_vars": {},
      "error_msg": "NoneType",
      "inference_task": "str",
      "instances": [
        "...",
        "... (1 items)"
      ],
      "lora_adapters": "NoneType",
      "model_name": "str",
      "model_server_id": "str",
      "model_server_name": "str",
      "model_server_tag": "str",
      "model_version_id": "NoneType",
      "model_version_name": "NoneType",
      "model_version_version": "NoneType",
      "registered_model_id": "NoneType",
      "registered_model_name": "NoneType",
      "replicas": "int",
      "resources": {
        "accelerator": "...",
        "accelerator_count": "...",
        "accelerator_slice": "...",
        "cpu": "...",
        "memory": "...",
        "number_of_nodes": "...",
        "storage": "...",
        "use_gpu": "..."
      },
      "status": "str",
      "status_detail": [
        "...",
        "... (2 items)"
      ],
      "supported_auto_scaling_metrics": [
        "...",
        "... (2 items)"
      ],
      "use_lora_adapters": "bool",
      "workspace_id": "str"
    },
    "... (28 items)"
  ],
  "meta": "NoneType",
  "paging": {
    "count": "int",
    "limit": "NoneType",
    "offset": "int"
  }
}
```

---

## Inference metrics meta

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/deployments/4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8/inference_metrics_meta`
The metric-id vocabulary + units.
**HTTP 200**

**Response (truncated):**

```json
{
  "metrics": [
    {
      "id": "successful_requests",
      "title": "Successful Requests"
    },
    {
      "id": "concurrent_requests",
      "title": "Concurrent Requests"
    },
    {
      "id": "response_time",
      "title": "Response Time",
      "unit": "second"
    }
  ],
  "range_start": "2026-09-22T06:05:05.058000Z"
}
```

**Field schema:**

```json
{
  "metrics": [
    {
      "id": "str",
      "title": "str"
    },
    "... (5 items)"
  ],
  "range_start": "str"
}
```

---

## Inference metrics (queue)

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/deployments/4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8/inference_metrics?metric_id=num_of_requests_waiting_in_queue&start=2026-09-29T06:00:00Z`
Prometheus-style series [[epoch, value], ...]. Requires metric_id + start (ISO).
**HTTP 200**

**Response (truncated):**

```json
[
  {
    "values": [
      [
        1790661600.0,
        "0"
      ],
      [
        1790661660.0,
        "0"
      ],
      [
        1790661720.0,
        "0"
      ]
    ]
  }
]
```

**Field schema:**

```json
[
  {
    "values": [
      [
        "...",
        "... (2 items)"
      ],
      "... (119 items)"
    ]
  },
  "... (1 items)"
]
```

---

## List workload runs

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/workloads/4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8/workload_runs`
Runs of a workload.
**HTTP 200**

**Response (truncated):**

```json
{
  "items": [
    {
      "_created_at": "2026-09-18T12:02:50.058000Z",
      "_lifecycle_stage": "active",
      "_updated_at": "2026-09-18T14:06:27.258000Z",
      "cluster_id": "c44e1386-6309-4ab5-8e4f-120a217bb1dd",
      "data_volumes": [
        {
          "_lifecycle_stage": "active",
          "id": "3143d34e-0d88-405c-9783-31cfb8a705b7",
          "name": "KimiK3"
        }
      ],
      "deployment": {
        "_lifecycle_stage": "active",
        "id": "4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8",
        "name": "ANOTHER: moonshotai/Kimi-K3",
        "status": "Ready"
      },
      "id": "bdaab232-7c78-40fb-8c84-a2c888486f25",
      "namespace": "adeo",
      "oip_type": "model_deployment",
      "resources": [
        {
          "accelerator_model": "b300",
          "accelerator_provider": "NVIDIA",
          "memory_gb": 1512,
          "num_accelerator": 8,
          "num_cpu": 32
        }
      ],
      "status_detail": [
        {
          "kind": "Deployment",
          "metadata": {
            "available": true,
            "available_replicas": 1,
            "progressing": true
          },
          "name": "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8",
          "node": "",
          "status": "Ready",
          "status_msg": "1/1 replicas ready"
        },
        {
          "kind": "Pod",
          "metadata": {
            "ready": true
          },
          "name": "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8-7dc6f96b5f-wrng9",
          "node": "adeo-gpu-b300-01",
          "status": "Running",
          "status_msg": ""
        }
      ],
      "total_resources": {
        "accelerator_model": "b300",
        "accelerator_provider": "NVIDIA",
        "memory_gb": 1512,
        "num_accelerator": 8,
        "num_cpu": 32
      },
      "type": "Deployment",
      "user_id": "558d12cb-0b76-4e88-b076-295c30f8692a",
      "user_name": "jyao",
      "workload_id": "4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8",
      "workload_status": "Running",
      "workspace": {
        "_created_at": "2025-12-17T06:29:57.922000Z",
        "_lifecycle_stage": "active",
        "_other_permissions": [],
        "_owner_id": "a5c034d7-a23d-4501-ad9a-726c808aceda",
        "_owner_permissions": [
          "read",
          "update",
          "delete"
        ],
        "_role_permissions": [
          "read",
          "update",
          "delete"
        ],
        "_roles": [],
        "_tenant_id": "adeo",
        "_updated_at": "2026-09-21T08:58:01.13300
```

**Field schema:**

```json
{
  "items": [
    {
      "_created_at": "str",
      "_lifecycle_stage": "str",
      "_updated_at": "str",
      "cluster_id": "str",
      "data_volumes": [
        "...",
        "... (1 items)"
      ],
      "deployment": {
        "_lifecycle_stage": "...",
        "id": "...",
        "name": "...",
        "status": "..."
      },
      "id": "str",
      "namespace": "str",
      "oip_type": "str",
      "resources": [
        "...",
        "... (1 items)"
      ],
      "status_detail": [
        "...",
        "... (2 items)"
      ],
      "total_resources": {
        "accelerator_model": "...",
        "accelerator_provider": "...",
        "memory_gb": "...",
        "num_accelerator": "...",
        "num_cpu": "..."
      },
      "type": "str",
      "user_id": "str",
      "user_name": "str",
      "workload_id": "str",
      "workload_status": "str",
      "workspace": {
        "_created_at": "...",
        "_lifecycle_stage": "...",
        "_other_permissions": "...",
        "_owner_id": "...",
        "_owner_permissions": "...",
        "_role_permissions": "...",
        "_roles": "...",
        "_tenant_id": "...",
        "_updated_at": "...",
        "_version": "...",
        "id": "...",
        "name": "...",
        "resource_borrowing_allowed": "...",
        "resources_allocated": "...",
        "resources_used": "...",
        "volcano_queue_name": "...",
        "workspace_resource_updater_last_updated_version": "..."
      },
      "workspace_id": "str"
    },
    "... (1 items)"
  ],
  "meta": {
    "count": "int",
    "offset": "int"
  },
  "paging": {
    "count": "int",
    "offset": "int"
  }
}
```

---

## Get workload run

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/workloads/4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8/workload_runs/bdaab232-7c78-40fb-8c84-a2c888486f25`
One run incl. status_detail[].

**Version difference: Al Ain `1.15.19` vs Abu Dhabi `1.7.1`.** Every `status_detail[]` entry above carries a `metadata` object (`ready`, `available`, `available_replicas`, `progressing`). Abu Dhabi's `1.7.1` returns entries with no `metadata` key at all, only `kind`, `name`, `node`, `status`, and `status_msg`. `metadata` is absent from that version's `StatusDetail` / `WorkloadStatusDetail` / `DeploymentInstance` schemas entirely, while Al Ain's declare it. The controller now treats `metadata` as optional and falls back to the entry's own `status`, which both versions populate, so a serving AD deployment is not read as not serving. Live payload: `oicm-aa-ad-cluster-interconnect/abudhabi-oicm-rest-api-export.md`.

**HTTP 200**

**Response (truncated):**

```json
{
  "_created_at": "2026-09-18T12:02:50.058000Z",
  "_lifecycle_stage": "active",
  "_updated_at": "2026-09-18T14:06:27.258000Z",
  "cluster_id": "c44e1386-6309-4ab5-8e4f-120a217bb1dd",
  "data_volumes": [],
  "id": "bdaab232-7c78-40fb-8c84-a2c888486f25",
  "namespace": "adeo",
  "oip_type": "model_deployment",
  "resources": [
    {
      "accelerator_model": "b300",
      "accelerator_provider": "NVIDIA",
      "memory_gb": 1512,
      "num_accelerator": 8,
      "num_cpu": 32
    }
  ],
  "status_detail": [
    {
      "kind": "Deployment",
      "metadata": {
        "available": true,
        "available_replicas": 1,
        "progressing": true
      },
      "name": "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8",
      "node": "",
      "status": "Ready",
      "status_msg": "1/1 replicas ready"
    },
    {
      "kind": "Pod",
      "metadata": {
        "ready": true
      },
      "name": "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8-7dc6f96b5f-wrng9",
      "node": "adeo-gpu-b300-01",
      "status": "Running",
      "status_msg": ""
    }
  ],
  "total_resources": {
    "accelerator_model": "b300",
    "accelerator_provider": "NVIDIA",
    "memory_gb": 1512,
    "num_accelerator": 8,
    "num_cpu": 32
  },
  "type": "Deployment",
  "user_id": "558d12cb-0b76-4e88-b076-295c30f8692a",
  "user_name": "jyao",
  "workload_id": "4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8",
  "workload_status": "Running",
  "workspace_id": "dfec2a9f-cc5c-4b7b-b608-990d3804e80c"
}
```

**Field schema:**

```json
{
  "_created_at": "str",
  "_lifecycle_stage": "str",
  "_updated_at": "str",
  "cluster_id": "str",
  "data_volumes": "[]",
  "id": "str",
  "namespace": "str",
  "oip_type": "str",
  "resources": [
    {
      "accelerator_model": "str",
      "accelerator_provider": "str",
      "memory_gb": "int",
      "num_accelerator": "int",
      "num_cpu": "int"
    },
    "... (1 items)"
  ],
  "status_detail": [
    {
      "kind": "str",
      "metadata": {
        "available": "...",
        "available_replicas": "...",
        "progressing": "..."
      },
      "name": "str",
      "node": "str",
      "status": "str",
      "status_msg": "str"
    },
    "... (2 items)"
  ],
  "total_resources": {
    "accelerator_model": "str",
    "accelerator_provider": "str",
    "memory_gb": "int",
    "num_accelerator": "int",
    "num_cpu": "int"
  },
  "type": "str",
  "user_id": "str",
  "user_name": "str",
  "workload_id": "str",
  "workload_status": "str",
  "workspace_id": "str"
}
```

---

## Workload run events

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/workloads/4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8/workload_runs/bdaab232-7c78-40fb-8c84-a2c888486f25/events`
Server-Sent-Events stream of K8s-style events for the run. The stream buffers and can exceed a short read timeout on a busy run, so the live probe timed out; the real captured stream is in `evidence/workload-run-events.sse`.
**HTTP 200 (SSE stream)**

**Captured event shape (`data:` line, JSON):**

```json
{"object_name":"j-061fbb9d-c138-4800-8f69-a091fcaed7d8","object_kind":"Deployment","count":1,"event_reason":"ScalingReplicaSet","event_type":"Normal","event_message":"Scaled up replica set j-061fbb9d-...-65f8bfc5d from 0 to 1","first_seen":"2026-09-02T12:35:58Z","last_seen":"2026-09-02T12:35:58Z"}
```

**Field schema (one `data:` payload):**

```json
{
  "object_name": "str",
  "object_kind": "str",
  "count": "int",
  "event_reason": "str",
  "event_type": "str",
  "event_message": "str",
  "first_seen": "str",
  "last_seen": "str"
}
```

**Observed `event_reason` vocabulary** (81 Pod + 8 ReplicaSet + 7 Deployment + 3 PVC events in the capture): `ScalingReplicaSet`, `Provisioning`, `ExternalProvisioning`, `ProvisioningSucceeded`, `SuccessfulCreate`, `SuccessfulDelete`, `Scheduled`, `FailedScheduling`, `SuccessfulAttachVolume`, `Pulling`, `Pulled`, `Created`, `Started`, `Killing`, `Unhealthy`, `BackOff`, `TaintManagerEviction`. This is the transition signal used to derive `starting` / `restarting` / `redeploying`.

---

## Workload run resources

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/workloads/4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8/workload_runs/bdaab232-7c78-40fb-8c84-a2c888486f25/resources`
Resource objects of the run.
**HTTP 200**

**Response (truncated):**

```json
{
  "items": [
    {
      "active": true,
      "id": "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8-7dc6f96b5f-wrng9",
      "kind": "Pod",
      "last_active": "2026-09-18T14:06:27.262000Z",
      "status": "Running"
    },
    {
      "active": false,
      "id": "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8-7596f9645b-5rns7",
      "kind": "Pod",
      "last_active": "2026-09-18T12:06:12.093000Z",
      "status": "Completed"
    }
  ]
}
```

**Field schema:**

```json
{
  "items": [
    {
      "active": "bool",
      "id": "str",
      "kind": "str",
      "last_active": "str",
      "status": "str"
    },
    "... (2 items)"
  ]
}
```

---

## Workload run workers

`GET /api/v1/workspaces/dfec2a9f-cc5c-4b7b-b608-990d3804e80c/workloads/4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8/workload_runs/bdaab232-7c78-40fb-8c84-a2c888486f25/workers`
Worker pods of the run.
**HTTP 200**

**Response (truncated):**

```json
{
  "workers": [
    "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8-7dc6f96b5f-wrng9",
    "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8-7596f9645b-5rns7"
  ],
  "items": [
    {
      "id": "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8-7dc6f96b5f-wrng9",
      "last_active": "2026-09-18T14:06:27.262000Z",
      "active": true,
      "kind": "Pod",
      "preferred": false,
      "status": "Running"
    },
    {
      "id": "j-4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8-7596f9645b-5rns7",
      "last_active": "2026-09-18T12:06:12.093000Z",
      "active": false,
      "kind": "Pod",
      "preferred": false,
      "status": "Completed"
    }
  ]
}
```

**Field schema:**

```json
{
  "workers": [
    "str",
    "... (2 items)"
  ],
  "items": [
    {
      "id": "str",
      "last_active": "str",
      "active": "bool",
      "kind": "str",
      "preferred": "bool",
      "status": "str"
    },
    "... (2 items)"
  ]
}
```

---

## Model servers summary

`GET /api/v1/model_servers/summary`
Serving-image catalog (NOT workspace-scoped).
**HTTP 200**

**Response (truncated):**

```json
{
  "data": [
    {
      "id": "393bc7c0-c084-44bb-8c60-d324a9491f33",
      "name": "llama.cpp",
      "tag": "b69721",
      "supported_devices": [
        "nvidia",
        "amd",
        "cpu"
      ],
      "task_types": [
        "Text Generation",
        "Image-Text-to-Text",
        "Text Embedding"
      ],
      "enabled": true,
      "family": "Llama CPP",
      "supported_auto_scaling_metrics": [],
      "multinode_inference_support": false
    },
    {
      "id": "05673fee-5194-41a6-bba5-6bce9ce83088",
      "name": "OI Serve",
      "tag": "1.0.0",
      "supported_devices": [
        "nvidia",
        "amd",
        "cpu"
      ],
      "task_types": [
        "Text Classification",
        "Text-to-Image",
        "Text-to-Speech"
      ],
      "enabled": true,
      "family": "OI Serve",
      "supported_auto_scaling_metrics": [],
      "multinode_inference_support": false
    },
    {
      "id": "2ed82784-d4f1-4261-bcaf-ea9beaf35f04",
      "name": "Ray Serve",
      "tag": "1.0.0py312",
      "supported_devices": [
        "nvidia",
        "amd",
        "cpu"
      ],
      "task_types": [
        "Generic"
      ],
      "enabled": true,
      "family": "Ray Serve",
      "supported_auto_scaling_metrics": [],
      "multinode_inference_support": false
    }
  ]
}
```

**Field schema:**

```json
{
  "data": [
    {
      "id": "str",
      "name": "str",
      "tag": "str",
      "supported_devices": [
        "...",
        "... (3 items)"
      ],
      "task_types": [
        "...",
        "... (3 items)"
      ],
      "enabled": "bool",
      "family": "str",
      "supported_auto_scaling_metrics": "[]",
      "multinode_inference_support": "bool"
    },
    "... (20 items)"
  ]
}
```

---
