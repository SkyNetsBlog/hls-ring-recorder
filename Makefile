-include config.local.mk

export IMAGE_REPO ?= lbrtx01/hls-ring-recorder
export IMAGE_TAG  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)
export HLS_STREAM ?= http://192.168.178.96:30900/hls/stream.m3u8
export NODE_SELECTOR_KEY   ?= kubernetes.io/hostname
export NODE_SELECTOR_VALUE ?= talos-k86-gbo
export SEGMENT_TIME        ?= 200
export SEGMENT_WRAP        ?= 400
export POLL_INTERVAL       ?= 15
export BLANK_TIMEOUT       ?= 60
export LUMA_THRESHOLD      ?= 15
export WEBHOOK_URL         ?= http://localhost:8081/ring-segments
export TERMINATION_GRACE   ?= 15
export PVC_SIZE            ?= 10Gi
FFMPEG_CFLAGS              ?= -O3

NGINX_URL          ?=
NTFY_URL           ?=
SYNC_OUTPUT_DIR    ?= $(CURDIR)/segments
SYNC_WORKERS       ?= 1
WHISPER_MODEL      ?= base

LOCAL_PATH_PROVISIONER_VERSION ?= v0.0.30
LOCAL_PATH_PROVISIONER_URL = https://raw.githubusercontent.com/rancher/local-path-provisioner/$(LOCAL_PATH_PROVISIONER_VERSION)/deploy/local-path-storage.yaml
LOCAL_PATH_PROVISIONER_MANIFEST = k8s/vendor/local-path-storage.yaml

.PHONY: docker-build docker-push k8s-apply k8s-rollout k8s-uninstall k8s-logs k8s-logs-recorder k8s-logs-nginx k8s-logs-ntfy k8s-setup k8s-vendor k8s-create-pull-secret clean deploy script-test script-sync script-subscribe help

docker-build:
	docker build --platform linux/amd64 --build-arg FFMPEG_CFLAGS="$(FFMPEG_CFLAGS)" -t $(IMAGE_REPO):$(IMAGE_TAG) docker/

docker-push:
	docker buildx build --platform linux/amd64 --build-arg FFMPEG_CFLAGS="$(FFMPEG_CFLAGS)" -t $(IMAGE_REPO):$(IMAGE_TAG) --push docker/

k8s/deployment.yaml: k8s/deployment.tmpl.yaml clean
	@mkdir -p $(dir $@)
	envsubst < $< > $@

k8s/pvc.yaml: k8s/pvc.tmpl.yaml
	envsubst < $< > $@

$(LOCAL_PATH_PROVISIONER_MANIFEST):
	@mkdir -p $(dir $@)
	curl -sSfL $(LOCAL_PATH_PROVISIONER_URL) -o $@

k8s-vendor: $(LOCAL_PATH_PROVISIONER_MANIFEST)

k8s-setup: $(LOCAL_PATH_PROVISIONER_MANIFEST)
	kubectl apply -f $(LOCAL_PATH_PROVISIONER_MANIFEST)
	kubectl label namespace local-path-storage \
		pod-security.kubernetes.io/enforce=privileged \
		pod-security.kubernetes.io/warn=privileged \
		pod-security.kubernetes.io/audit=privileged \
		--overwrite
	kubectl patch deployment local-path-provisioner -n local-path-storage --type=json \
		-p='[{"op":"add","path":"/spec/template/spec/tolerations","value":[{"key":"node-role.kubernetes.io/control-plane","operator":"Exists","effect":"NoSchedule"}]}]'
	kubectl rollout status deployment/local-path-provisioner -n local-path-storage

k8s-apply: k8s/pvc.yaml k8s/deployment.yaml
	kubectl apply -f k8s/namespace.yaml -f k8s/pvc.yaml -f k8s/nginx-config.yaml -f k8s/service.yaml -f k8s/deployment.yaml

k8s-rollout:
	kubectl rollout restart deployment/hls-ring-recorder -n recorder
	kubectl rollout status deployment/hls-ring-recorder -n recorder

deploy: docker-push k8s-apply k8s-rollout

k8s-create-pull-secret:
	@[ -n "$(DOCKER_USER)" ] || { echo "Error: DOCKER_USER is not set"; exit 1; }
	@[ -n "$(DOCKER_TOKEN)" ] || { echo "Error: DOCKER_TOKEN is not set"; exit 1; }
	kubectl create secret docker-registry dockerhub \
		--namespace recorder \
		--docker-server=https://index.docker.io/v1/ \
		--docker-username=$(DOCKER_USER) \
		--docker-password=$(DOCKER_TOKEN) \
		--dry-run=client -o yaml | kubectl apply -f -

k8s-uninstall:
	kubectl delete namespace recorder

k8s-logs:
	kubectl logs -l app=hls-ring-recorder --tail=100 -n recorder -f

k8s-logs-recorder:
	kubectl logs -l app=hls-ring-recorder --tail=100 -n recorder -c hls-ring-recorder -f

k8s-logs-nginx:
	kubectl logs -l app=hls-ring-recorder --tail=100 -n recorder -c nginx -f

k8s-logs-ntfy:
	kubectl logs -l app=hls-ring-recorder --tail=100 -n recorder -c ntfy -f

script-test:
	cd scripts/segment-batch-fetcher && uv run --group dev pytest tests/ -v

script-sync:
	cd scripts/segment-batch-fetcher && uv run sync.py $(NGINX_URL) $(SYNC_OUTPUT_DIR) --workers $(SYNC_WORKERS)

script-subscribe:
	cd scripts/webhook-subscriber-example && uv run tester.py $(NTFY_URL) --nginx-url $(NGINX_URL) --model $(WHISPER_MODEL)

clean:
	@rm -f k8s/deployment.yaml

help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "  deploy           Build + push image, apply manifests, force rollout"
	@echo "  docker-build     Build image locally (no push)"
	@echo "  docker-push      Build and push multi-arch image to Docker Hub"
	@echo "  k8s-setup        Install local-path-provisioner storage class (first-time only)"
	@echo "  k8s-apply        Generate k8s/deployment.yaml and apply all manifests"
	@echo "  k8s-rollout      Restart the deployment and wait for rollout"
	@echo "  k8s-logs         Tail logs from all pod containers"
	@echo "  k8s-logs-recorder  Tail logs from the recorder container only"
	@echo "  k8s-logs-nginx   Tail logs from the nginx container only"
	@echo "  k8s-logs-ntfy    Tail logs from the ntfy container only"
	@echo "  k8s-create-pull-secret  Create/update the dockerhub image pull secret (requires DOCKER_USER and DOCKER_TOKEN)"
	@echo "  k8s-uninstall    Delete the recorder namespace and all its resources"
	@echo "  k8s-vendor       Download the local-path-provisioner manifest"
	@echo "  script-test      Run Python unit tests (segment-batch-fetcher)"
	@echo "  script-sync      Download .ts segments  (requires NGINX_URL; optional SYNC_OUTPUT_DIR, SYNC_WORKERS)"
	@echo "  script-subscribe Subscribe to ntfy and transcribe segments  (requires NGINX_URL, NTFY_URL; optional WHISPER_MODEL)"
	@echo "  clean            Remove generated k8s/deployment.yaml"