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

{{- define "crossfact.usesBackupBucket" -}}
{{- if or .Values.backup.enabled .Values.restore.enabled -}}true{{- end -}}
{{- end -}}

{{/* Backups and restores need the bucket and their own credential; refuse to render without them. */}}
{{- define "crossfact.validateBackup" -}}
{{- if include "crossfact.usesBackupBucket" . -}}
{{- $b := .Values.backup -}}
{{- range $k := list "bucket" "namespace" "region" -}}
{{- if not (index $b $k) -}}
{{- fail (printf "backup.%s is required while backup.enabled or restore.enabled is true (or set both false)" $k) -}}
{{- end -}}
{{- end -}}
{{- range $k := list "tenancy" "user" "secretName" -}}
{{- if not (index $b.auth $k) -}}
{{- fail (printf "backup.auth.%s is required while backup.enabled or restore.enabled is true: backups sign in as their own OCI user (the deploy repo's foundation stack prints them)" $k) -}}
{{- end -}}
{{- end -}}
{{- end -}}
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

{{/* The backup user's private key. Only maintenance containers mount it; the app container never does. */}}
{{- define "crossfact.backupKeyVolume" -}}
{{- if include "crossfact.usesBackupBucket" . -}}
- name: backup-key
  secret:
    secretName: {{ .Values.backup.auth.secretName }}
    defaultMode: 0440
{{- end -}}
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
  {{- include "crossfact.backupKeyVolume" . | nindent 2 }}
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
  {{- if include "crossfact.usesBackupBucket" . }}
  - {name: backup-key, mountPath: /var/run/secrets/crossfact-backup, readOnly: true}
  {{- end }}
{{- end -}}
