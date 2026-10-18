{{/*
Expand the name of the chart.
*/}}
{{- define "proxmox-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Create a default fully qualified app name.
Always includes chart name to distinguish from other operators (e.g., proxmox-operator vs aws-operator).
*/}}
{{- define "proxmox-operator.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end -}}

{{/*
Proxmox credentials Secret name for an instance.
Usage: include "proxmox-operator.credentialsSecretName" (dict "root" $ "instance" .)
Prefers instance.existingSecret when set (CLI / ExternalSecrets create the Secret).
*/}}
{{- define "proxmox-operator.credentialsSecretName" -}}
{{- if .instance.existingSecret -}}
{{- .instance.existingSecret -}}
{{- else -}}
{{- printf "%s-proxmox-credentials-%s" (include "proxmox-operator.fullname" .root) .instance.id -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "proxmox-operator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "proxmox-operator.labels" -}}
helm.sh/chart: {{ include "proxmox-operator.chart" . }}
{{ include "proxmox-operator.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ include "proxmox-operator.name" . }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "proxmox-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "proxmox-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Component labels for operator
*/}}
{{- define "proxmox-operator.operator.labels" -}}
{{ include "proxmox-operator.labels" . }}
app.kubernetes.io/component: operator
{{- end -}}

{{/*
Component labels for prometheus exporter
*/}}
{{- define "proxmox-operator.prometheusExporter.labels" -}}
{{ include "proxmox-operator.labels" . }}
app.kubernetes.io/component: metrics
{{- end -}}

{{/*
Selector labels for prometheus exporter
*/}}
{{- define "proxmox-operator.prometheusExporter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "proxmox-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: metrics
{{- end -}}

{{/*
Component labels for VNC gateway
*/}}
{{- define "proxmox-operator.vncGateway.labels" -}}
{{ include "proxmox-operator.labels" . }}
app.kubernetes.io/component: vnc-gateway
{{- end -}}

{{/*
Selector labels for VNC gateway
*/}}
{{- define "proxmox-operator.vncGateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "proxmox-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: vnc-gateway
{{- end -}}

{{/*
Service account name
*/}}
{{- define "proxmox-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.name -}}
{{ .Values.serviceAccount.name }}
{{- else -}}
{{ include "proxmox-operator.fullname" . }}
{{- end -}}
{{- end -}}

{{/*
Namespace for hook Jobs and script Secrets (not the VM CR namespace).
*/}}
{{- define "proxmox-operator.hookNamespace" -}}
{{- if .Values.hooks.jobNamespace -}}
{{- .Values.hooks.jobNamespace | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-hooks" .Release.Namespace | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Provider-owned Secret object name mounted into hook Jobs.
Unset key → default; explicit empty string disables injection.
*/}}
{{- define "proxmox-operator.hooksProviderSecretName" -}}
{{- if hasKey (.Values.hooks | default dict) "providerSecretName" -}}
{{- .Values.hooks.providerSecretName -}}
{{- else -}}
noderings-hook-secrets
{{- end -}}
{{- end -}}

{{/*
Extract host from Proxmox URL
Input: URL string (e.g., "https://192.0.2.10:8006")
*/}}
{{- define "proxmox-operator.parseHost" -}}
{{- $url := . | toString | replace "https://" "" | replace "http://" "" -}}
{{- $parts := splitList ":" $url -}}
{{- if $parts -}}
{{- (index $parts 0) -}}
{{- else -}}
{{- $url -}}
{{- end -}}
{{- end -}}

{{/*
Extract port from Proxmox URL (defaults to 8006 if not specified)
Input: URL string (e.g., "https://192.0.2.10:8006")
*/}}
{{- define "proxmox-operator.parsePort" -}}
{{- $url := . | toString | replace "https://" "" | replace "http://" "" -}}
{{- $parts := splitList ":" $url -}}
{{- if and $parts (gt (len $parts) 1) -}}
{{- (index $parts 1) -}}
{{- else -}}
8006
{{- end -}}
{{- end -}}

{{/*
Build target parameter for PVE exporter (host:port format)
Input: URL string (e.g., "https://192.0.2.10:8006")
*/}}
{{- define "proxmox-operator.buildTarget" -}}
{{- . | toString | replace "https://" "" | replace "http://" "" -}}
{{- end -}}

{{/*
Pod security context defaults
*/}}
{{- define "proxmox-operator.podSecurityContext" -}}
{{- $ctx := .context -}}
{{- $overrides := .overrides | default dict -}}
{{- $defaults := $ctx.Values.podSecurityContext | default dict -}}
{{- mergeOverwrite (deepCopy $defaults) $overrides | toYaml -}}
{{- end -}}

{{/*
Container security context defaults
*/}}
{{- define "proxmox-operator.containerSecurityContext" -}}
{{- $ctx := .context -}}
{{- $overrides := .overrides | default dict -}}
{{- $defaults := $ctx.Values.containerSecurityContext | default dict -}}
{{- mergeOverwrite (deepCopy $defaults) $overrides | toYaml -}}
{{- end -}}
