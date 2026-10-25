{{- define "crossfact.name" -}}crossfact{{- end -}}

{{- define "crossfact.labels" -}}
app.kubernetes.io/name: crossfact
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "crossfact.selector" -}}
app.kubernetes.io/name: crossfact
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "crossfact.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}
{{- end -}}

{{- define "crossfact.secretName" -}}
{{- if .Values.secrets.existingSecret -}}{{ .Values.secrets.existingSecret }}{{- else -}}crossfact-secrets{{- end -}}
{{- end -}}

{{- define "crossfact.claimName" -}}
{{- if .Values.persistence.existingClaim -}}{{ .Values.persistence.existingClaim }}{{- else -}}crossfact-data{{- end -}}
{{- end -}}

{{/* Volumes shared by the app and its maintenance jobs. */}}
{{- define "crossfact.volumes" -}}
- name: data
  persistentVolumeClaim:
    claimName: {{ include "crossfact.claimName" . }}
- name: config
  configMap:
    name: crossfact-config
- name: tmp
  emptyDir:
    sizeLimit: 1Gi
{{- end -}}

{{- define "crossfact.jobPod" -}}
restartPolicy: Never
automountServiceAccountToken: false
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
volumes:
  {{- include "crossfact.volumes" . | nindent 2 }}
{{- end -}}

{{- define "crossfact.jobContainer" -}}
image: {{ include "crossfact.image" . }}
imagePullPolicy: {{ .Values.image.pullPolicy }}
env:
  - name: CROSSFACT_CONFIG
    value: /etc/crossfact/config.yaml
securityContext:
  {{- toYaml .Values.securityContext | nindent 2 }}
resources:
  {{- toYaml .Values.jobResources | nindent 2 }}
volumeMounts:
  - {name: data, mountPath: /data}
  - {name: config, mountPath: /etc/crossfact, readOnly: true}
  - {name: tmp, mountPath: /tmp}
{{- end -}}
