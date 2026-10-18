{{/*
Expand the name of the chart.
*/}}
{{- define "solusvm-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "solusvm-operator.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end -}}

{{/*
SolusVM credentials Secret name for an instance.
Usage: include "solusvm-operator.credentialsSecretName" (dict "root" $ "instance" .)
Prefers instance.existingSecret when set (CLI / ExternalSecrets create the Secret).
*/}}
{{- define "solusvm-operator.credentialsSecretName" -}}
{{- if .instance.existingSecret -}}
{{- .instance.existingSecret -}}
{{- else -}}
{{- printf "%s-solusvm-credentials-%s" (include "solusvm-operator.fullname" .root) .instance.id -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "solusvm-operator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "solusvm-operator.labels" -}}
helm.sh/chart: {{ include "solusvm-operator.chart" . }}
{{ include "solusvm-operator.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ include "solusvm-operator.name" . }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "solusvm-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "solusvm-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Component labels for operator
*/}}
{{- define "solusvm-operator.operator.labels" -}}
{{ include "solusvm-operator.labels" . }}
app.kubernetes.io/component: operator
{{- end -}}

{{/*
Component labels for prometheus exporter
*/}}
{{- define "solusvm-operator.prometheusExporter.labels" -}}
{{ include "solusvm-operator.labels" . }}
app.kubernetes.io/component: metrics
{{- end -}}

{{/*
Selector labels for prometheus exporter
*/}}
{{- define "solusvm-operator.prometheusExporter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "solusvm-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: metrics
{{- end -}}

{{/*
Component labels for VNC gateway
*/}}
{{- define "solusvm-operator.vncGateway.labels" -}}
{{ include "solusvm-operator.labels" . }}
app.kubernetes.io/component: vnc-gateway
{{- end -}}

{{/*
Selector labels for VNC gateway
*/}}
{{- define "solusvm-operator.vncGateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "solusvm-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: vnc-gateway
{{- end -}}

{{/*
Service account name
*/}}
{{- define "solusvm-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.name -}}
{{ .Values.serviceAccount.name }}
{{- else -}}
{{ include "solusvm-operator.fullname" . }}
{{- end -}}
{{- end -}}

{{/*
Namespace for hook Jobs and script Secrets (not the VM CR namespace).
*/}}
{{- define "solusvm-operator.hookNamespace" -}}
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
{{- define "solusvm-operator.hooksProviderSecretName" -}}
{{- if hasKey (.Values.hooks | default dict) "providerSecretName" -}}
{{- .Values.hooks.providerSecretName -}}
{{- else -}}
noderings-hook-secrets
{{- end -}}
{{- end -}}

{{/*
Extract host from SolusVM URL (strip scheme)
*/}}
{{- define "solusvm-operator.parseHost" -}}
{{- $url := . | toString | replace "https://" "" | replace "http://" "" -}}
{{- $parts := splitList ":" $url -}}
{{- if $parts -}}
{{- (index $parts 0) -}}
{{- else -}}
{{- $url -}}
{{- end -}}
{{- end -}}

{{/*
Pod security context defaults
*/}}
{{- define "solusvm-operator.podSecurityContext" -}}
{{- $ctx := .context -}}
{{- $overrides := .overrides | default dict -}}
{{- $defaults := $ctx.Values.podSecurityContext | default dict -}}
{{- mergeOverwrite (deepCopy $defaults) $overrides | toYaml -}}
{{- end -}}

{{/*
Container security context defaults
*/}}
{{- define "solusvm-operator.containerSecurityContext" -}}
{{- $ctx := .context -}}
{{- $overrides := .overrides | default dict -}}
{{- $defaults := $ctx.Values.containerSecurityContext | default dict -}}
{{- mergeOverwrite (deepCopy $defaults) $overrides | toYaml -}}
{{- end -}}
