{{- define "umc-product-server.name" -}}
{{- .Chart.Name -}}
{{- end }}

{{- define "umc-product-server.fullname" -}}
{{- /*
  기본은 차트 이름 그대로다. prod·dev 는 네임스페이스가 달라 이름이 같아도 충돌하지 않는다.
  PR 프리뷰는 여러 개가 preview 네임스페이스 하나에 들어가므로 fullnameOverride 로 구분한다.
  .Release.Name 을 쓰지 않는 이유 — 그러면 prod(umc-product-server)는 그대로지만
  dev(dev-umc-product-server)의 리소스 이름이 바뀌어 기존 워크로드가 재생성된다.
*/ -}}
{{- default .Chart.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "umc-product-server.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ include "umc-product-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
environment: {{ required "environment 는 필수 (prod | dev | preview)" .Values.environment }}
{{- end }}

{{- define "umc-product-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "umc-product-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
