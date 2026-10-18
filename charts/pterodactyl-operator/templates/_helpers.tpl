{{- define "pterodactyl-operator.fullname" -}}
{{- printf "%s-pterodactyl-operator" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "pterodactyl-operator.labels" -}}
app.kubernetes.io/name: pterodactyl-operator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "pterodactyl-operator.selectorLabels" -}}
app.kubernetes.io/name: pterodactyl-operator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "pterodactyl-operator.consoleGateway.labels" -}}
app.kubernetes.io/name: pterodactyl-operator
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: console-gateway
{{- end -}}

{{- define "pterodactyl-operator.consoleGateway.selectorLabels" -}}
app.kubernetes.io/name: pterodactyl-operator
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: console-gateway
{{- end -}}
