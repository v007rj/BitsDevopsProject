pipeline {
    agent { label 'aceest-linux' }
    options {
        disableConcurrentBuilds()
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }
    triggers { pollSCM('H/5 * * * *') }
    parameters {
        string(name: 'IMAGE_REPOSITORY', defaultValue: '', description: 'Your public Docker Hub repository, e.g. username/aceest-fitness')
        string(name: 'KUBE_CONTEXT', defaultValue: 'minikube', description: 'Context in the Jenkins kubeconfig credential')
        choice(name: 'STRATEGY', choices: ['rolling', 'blue-green', 'canary', 'shadow', 'ab'])
        booleanParam(name: 'DEPLOY', defaultValue: false, description: 'Deploy main after gates; initialize cluster first')
    }
    environment {
        LOCAL_IMAGE = "aceest-ci:${BUILD_NUMBER}"
        DOCKER_CONFIG = "${WORKSPACE}/.docker-auth"
    }
    stages {
        stage('Checkout') {
            steps {
                checkout scm
                script {
                    env.RELEASE_VERSION = readFile('VERSION').trim()
                    env.REVISION = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
                    env.RELEASE_TAG = "${env.RELEASE_VERSION}-build.${env.BUILD_NUMBER}-${env.REVISION.take(12)}"
                }
                sh 'mkdir -p reports artifacts'
            }
        }
        stage('Build') {
            steps {
                sh '''
                    docker build --build-arg APP_VERSION="$RELEASE_VERSION" \
                      --build-arg VCS_REF="$REVISION" --tag "$LOCAL_IMAGE" .
                '''
            }
        }
        stage('Container tests and lint') {
            steps {
                sh 'docker run --rm "$LOCAL_IMAGE" ruff check .'
                sh '''
                    docker run --rm --user "$(id -u):$(id -g)" \
                      -e DATABASE_PATH=/tmp/aceest.sqlite \
                      -e COVERAGE_FILE=/tmp/.coverage \
                      -v "$WORKSPACE/reports:/reports" "$LOCAL_IMAGE" \
                      pytest --junitxml=/reports/pytest.xml \
                        --cov-report=xml:/reports/coverage.xml \
                        --cov-report=json:/reports/coverage.json
                    sed -i 's|<source>/app</source>|<source>.</source>|g' reports/coverage.xml
                '''
            }
            post { always { junit 'reports/pytest.xml' } }
        }
        stage('Routing integration') {
            steps {
                sh '''
                    if python3 -m scripts.check_router --image "$LOCAL_IMAGE" > reports/routing.txt; then
                        cat reports/routing.txt
                    else
                        cat reports/routing.txt
                        exit 1
                    fi
                '''
            }
        }
        stage('SonarQube analysis') {
            steps {
                script {
                    def scanner = tool 'SonarScanner'
                    withSonarQubeEnv('aceest-sonarqube') {
                        withEnv(["SCANNER_HOME=${scanner}"]) {
                            sh '''
                                set +x
                                export SONAR_TOKEN="$SONAR_AUTH_TOKEN"
                                "$SCANNER_HOME/bin/sonar-scanner" \
                                  -Dsonar.projectVersion="$RELEASE_VERSION" \
                                  -Dsonar.scm.revision="$REVISION"
                            '''
                        }
                    }
                }
            }
        }
        stage('Quality gate') {
            steps {
                timeout(time: 6, unit: 'MINUTES') {
                    waitForQualityGate abortPipeline: false
                }
                withSonarQubeEnv('aceest-sonarqube') {
                    withCredentials([string(credentialsId: 'sonarqube-token', variable: 'SONAR_TOKEN')]) {
                        sh 'python3 -m scripts.sonar_report'
                    }
                }
            }
        }
        stage('Publish immutable image') {
            when { branch 'main' }
            steps {
                sh 'test -n "$IMAGE_REPOSITORY"'
                withCredentials([usernamePassword(credentialsId: 'dockerhub', usernameVariable: 'REGISTRY_USER', passwordVariable: 'REGISTRY_TOKEN')]) {
                    sh '''
                        set +x
                        printf '%s' "$REGISTRY_TOKEN" | docker login --username "$REGISTRY_USER" --password-stdin
                        docker tag "$LOCAL_IMAGE" "$IMAGE_REPOSITORY:$RELEASE_TAG"
                        docker push "$IMAGE_REPOSITORY:$RELEASE_TAG"
                        docker inspect --format='{{index .RepoDigests 0}}' "$IMAGE_REPOSITORY:$RELEASE_TAG" > artifacts/image-digest.txt
                    '''
                }
            }
        }
        stage('Deploy and verify; rollback on failure') {
            when {
                allOf { branch 'main'; expression { params.DEPLOY } }
            }
            steps {
                withCredentials([file(credentialsId: 'aceest-kubeconfig', variable: 'KUBECONFIG')]) {
                    sh '''
                        python3 -m scripts.deploy deploy --context "$KUBE_CONTEXT" \
                          --image "$(cat artifacts/image-digest.txt)" \
                          --version "$RELEASE_TAG" --strategy "$STRATEGY" \
                          --evidence artifacts
                    '''
                }
            }
        }
    }
    post {
        always {
            archiveArtifacts artifacts: 'reports/*,artifacts/*,.scannerwork/report-task.txt', allowEmptyArchive: true
            script {
                if (fileExists('.docker-auth/config.json')) {
                    sh 'docker logout'
                }
            }
            sh 'rm -rf "$WORKSPACE/.docker-auth"'
        }
    }
}
