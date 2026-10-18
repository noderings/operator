{{/*
Expand the name of the chart.
*/}}
{{- define "virtfusion-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "virtfusion-operator.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end -}}

{{/*
VirtFusion credentials Secret name for an instance.
Usage: include "virtfusion-operator.credentialsSecretName" (dict "root" $ "instance" .)
Prefers instance.existingSecret when set (CLI / ExternalSecrets create the Secret).
*/}}
{{- define "virtfusion-operator.credentialsSecretName" -}}
{{- if .instance.existingSecret -}}
{{- .instance.existingSecret -}}
{{- else -}}
{{- printf "%s-virtfusion-credentials-%s" (include "virtfusion-operator.fullname" .root) .instance.id -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "virtfusion-operator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "virtfusion-operator.labels" -}}
helm.sh/chart: {{ include "virtfusion-operator.chart" . }}
{{ include "virtfusion-operator.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ include "virtfusion-operator.name" . }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "virtfusion-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "virtfusion-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Component labels for operator
*/}}
{{- define "virtfusion-operator.operator.labels" -}}
{{ include "virtfusion-operator.labels" . }}
app.kubernetes.io/component: operator
{{- end -}}

{{/*
Component labels for prometheus exporter
*/}}
{{- define "virtfusion-operator.prometheusExporter.labels" -}}
{{ include "virtfusion-operator.labels" . }}
app.kubernetes.io/component: metrics
{{- end -}}

{{/*
Selector labels for prometheus exporter
*/}}
{{- define "virtfusion-operator.prometheusExporter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "virtfusion-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: metrics
{{- end -}}

{{/*
Component labels for VNC gateway
*/}}
{{- define "virtfusion-operator.vncGateway.labels" -}}
{{ include "virtfusion-operator.labels" . }}
app.kubernetes.io/component: vnc-gateway
{{- end -}}

{{/*
Selector labels for VNC gateway
*/}}
{{- define "virtfusion-operator.vncGateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "virtfusion-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: vnc-gateway
{{- end -}}

{{/*
Service account name
*/}}
{{- define "virtfusion-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.name -}}
{{ .Values.serviceAccount.name }}
{{- else -}}
{{ include "virtfusion-operator.fullname" . }}
{{- end -}}
{{- end -}}

{{/*
Namespace for hook Jobs and script Secrets (not the VM CR namespace).
*/}}
{{- define "virtfusion-operator.hookNamespace" -}}
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
{{- define "virtfusion-operator.hooksProviderSecretName" -}}
{{- if hasKey (.Values.hooks | default dict) "providerSecretName" -}}
{{- .Values.hooks.providerSecretName -}}
{{- else -}}
noderings-hook-secrets
{{- end -}}
{{- end -}}

{{/*
Extract host from VirtFusion URL (strip scheme)
*/}}
{{- define "virtfusion-operator.parseHost" -}}
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
{{- define "virtfusion-operator.podSecurityContext" -}}
{{- $ctx := .context -}}
{{- $overrides := .overrides | default dict -}}
{{- $defaults := $ctx.Values.podSecurityContext | default dict -}}
{{- mergeOverwrite (deepCopy $defaults) $overrides | toYaml -}}
{{- end -}}

{{/*
Container security context defaults
*/}}
{{- define "virtfusion-operator.containerSecurityContext" -}}
{{- $ctx := .context -}}
{{- $overrides := .overrides | default dict -}}
{{- $defaults := $ctx.Values.containerSecurityContext | default dict -}}
{{- mergeOverwrite (deepCopy $defaults) $overrides | toYaml -}}
{{- end -}}
