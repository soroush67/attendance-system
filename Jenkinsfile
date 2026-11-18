// Attendance v1.8.5 candidate: first/last daily and period presence reporting.
// Production deployment is OFF by default and needs a second manual gate.
pipeline {
    agent { label 'inf-18-jenk-3' }

    options {
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 60, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30', artifactNumToKeepStr: '30'))
        skipDefaultCheckout(true)
    }

    parameters {
        booleanParam(name: 'DEPLOY_PRODUCTION', defaultValue: false,
                     description: 'OFF by default. Requires additional manual approval for web-only deployment.')
    }

    environment {
        APPLICATION_REPOSITORY = 'https://gitlab.hamainsurance.net/opration/attendance-system.git'
        APPLICATION_BRANCH = 'develop'
        NEXUS_DOCKER_HOSTED = 'docker-hosted.hamainsurance.net'
        IMAGE_REPOSITORY = 'docker-hosted.hamainsurance.net/attendance-web'
        PIP_INDEX_URL = 'https://nexus.hamainsurance.net/repository/py-proxy/simple'
        DEPLOY_HOST = '192.168.11.10'
        DEPLOY_SSH_PORT = '22'
    }

    stages {
        stage('Checkout') {
            steps {
                deleteDir()
                git branch: "${env.APPLICATION_BRANCH}",
                    credentialsId: 'gitlab-http-clone',
                    url: "${env.APPLICATION_REPOSITORY}"
                sh '''#!/usr/bin/env bash
set -euo pipefail
test -s app/main.py
test -s app/backup_status.py
test -s app/Dockerfile
test -s app/.dockerignore
test -s app/templates/manager-attendance-template.xlsx
test -s tests/fixtures/manager-excel-150-valid.xlsx
test -s tests/fixtures/manager-excel-duplicate.xlsx
test -s tests/fixtures/manager-excel-formula.xlsx
test -s compose.backup-status.yml
test -s ci/remote_release.py
test -s ci/digest_ref.py
test -s scripts/run_offline_tests.py
test -s app/migrations/versions/0001_initial_schema.py
test ! -e .env
test ! -e app/certs/company-root-ca.crt
python3 scripts/check_project.py .
python3 -m py_compile app/main.py app/backup_status.py ci/digest_ref.py ci/remote_release.py
python3 -m unittest discover -s tests -p 'test_backup*.py' -v
python3 -m unittest discover -s tests -p 'test_ci_release.py' -v
'''
            }
        }

        stage('Image Tag') {
            steps {
                script {
                    def sha8 = sh(script: 'git rev-parse --short=8 HEAD', returnStdout: true).trim()
                    env.FULL_IMAGE_NAME = "${env.IMAGE_REPOSITORY}:${env.APPLICATION_BRANCH}-${env.BUILD_NUMBER}-${sha8}"
                    writeFile file: 'image-tag.txt', text: "${env.FULL_IMAGE_NAME}\n"
                }
            }
        }

        stage('Build') {
            steps {
                withCredentials([file(credentialsId: 'internal-ca-cert-file', variable: 'CA_CERT_FILE')]) {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
set +x
test -s "$CA_CERT_FILE"
mkdir -p app/certs
trap 'rm -f app/certs/company-root-ca.crt' EXIT
cp "$CA_CERT_FILE" app/certs/company-root-ca.crt
chmod 600 app/certs/company-root-ca.crt
docker build --file app/Dockerfile --tag "$FULL_IMAGE_NAME" \
    --build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" app
'''
                }
            }
        }

        stage('Offline Tests') {
            steps {
                sh '''#!/usr/bin/env bash
set -euo pipefail
docker run --rm --network none --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777 \
    --memory 768m --pids-limit 256 \
    --cap-drop ALL --security-opt no-new-privileges \
    -v "$PWD/tests:/tests:ro" \
    -v "$PWD/ops:/ops:ro" \
    -v "$PWD/ci:/ci:ro" \
    -v "$PWD/scripts:/scripts:ro" \
    --entrypoint python "$FULL_IMAGE_NAME" \
    /scripts/run_offline_tests.py /tests
'''
            }
        }

        stage('Push Image') {
            steps {
                withCredentials([usernamePassword(credentialsId: 'nexus-docker-credential',
                    usernameVariable: 'NEXUS_USERNAME', passwordVariable: 'NEXUS_PASSWORD')]) {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
set +x
export DOCKER_CONFIG="$(mktemp -d)"
trap 'docker logout "$NEXUS_DOCKER_HOSTED" >/dev/null 2>&1 || true; rm -f "$DOCKER_CONFIG/config.json"; rmdir "$DOCKER_CONFIG" 2>/dev/null || true' EXIT
printf '%s' "$NEXUS_PASSWORD" | docker login "$NEXUS_DOCKER_HOSTED" \
    --username "$NEXUS_USERNAME" --password-stdin >/dev/null
docker push "$FULL_IMAGE_NAME" >/dev/null
python3 ci/digest_ref.py "$FULL_IMAGE_NAME" > release-ref.txt
'''
                }
            }
        }

        stage('Manual Production Approval') {
            when { expression { return params.DEPLOY_PRODUCTION } }
            steps {
                timeout(time: 10, unit: 'MINUTES') {
                    input message: 'Approve brief outage of Attendance WEB ONLY? Database/RustFS are untouched.',
                          ok: 'Deploy verified image'
                }
            }
        }

        stage('Production Web Only') {
            when { expression { return params.DEPLOY_PRODUCTION } }
            steps {
                withCredentials([
                    sshUserPrivateKey(credentialsId: 'stage-deploy-ssh',
                        keyFileVariable: 'SSH_PRIVATE_KEY', usernameVariable: 'DEPLOY_USER'),
                    usernamePassword(credentialsId: 'nexus-docker-credential',
                        usernameVariable: 'NEXUS_USERNAME', passwordVariable: 'NEXUS_PASSWORD')
                ]) {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
set +x
RELEASE_REF="$(cat release-ref.txt)"
[[ "$RELEASE_REF" =~ ^docker-hosted[.]hamainsurance[.]net/attendance-web@sha256:[a-f0-9]{64}$ ]]
REMOTE="${DEPLOY_USER}@${DEPLOY_HOST}"
SSH_OPTS=(-i "$SSH_PRIVATE_KEY" -p "$DEPLOY_SSH_PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10)
SCP_OPTS=(-i "$SSH_PRIVATE_KEY" -P "$DEPLOY_SSH_PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10)
REMOTE_DIR="$(ssh "${SSH_OPTS[@]}" "$REMOTE" 'mktemp -d /tmp/attendance-ci.XXXXXXXXXX')"
[[ "$REMOTE_DIR" =~ ^/tmp/attendance-ci[.][A-Za-z0-9]{10}$ ]]
cleanup() {
  ssh "${SSH_OPTS[@]}" "$REMOTE" "rm -f '$REMOTE_DIR/remote_release.py'; rmdir '$REMOTE_DIR'" >/dev/null 2>&1 || true
}
trap cleanup EXIT
scp "${SCP_OPTS[@]}" ci/remote_release.py "$REMOTE:$REMOTE_DIR/remote_release.py" >/dev/null
ssh "${SSH_OPTS[@]}" "$REMOTE" "python3 '$REMOTE_DIR/remote_release.py' preflight '$RELEASE_REF'"
printf '%s' "$NEXUS_PASSWORD" | ssh "${SSH_OPTS[@]}" "$REMOTE" \
  "python3 '$REMOTE_DIR/remote_release.py' deploy '$RELEASE_REF' '$NEXUS_USERNAME'"
'''
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'image-tag.txt,release-ref.txt', allowEmptyArchive: true, fingerprint: true
            sh 'rm -f app/certs/company-root-ca.crt || true'
        }
        success { echo 'Attendance build/push done; deployment requires DEPLOY_PRODUCTION and manual approval.' }
        failure { echo 'FAIL: Inspect stage logs; release script never alters PostgreSQL/RustFS.' }
    }
}
