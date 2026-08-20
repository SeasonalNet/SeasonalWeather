from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_provisions_voicetext_runtime_dirs_under_canonical_state_root() -> None:
    bootstrap = (REPO_ROOT / "scripts/00-bootstrap.sh").read_text()

    assert 'VTP_STATE_BASE="${SEASONALWEATHER_DATA_BASE:-/var/lib/seasonalweather}"' in bootstrap
    assert 'install -d -o seasonalweather -g seasonalweather "${VTP_STATE_BASE}/voices"' in bootstrap
    assert 'install -d -o seasonalweather -g seasonalweather -m 2775 "${VTP_BASE}"' in bootstrap
    assert 'install -d -o seasonalweather -g seasonalweather -m 2775 "${VTP_BIN_DIR}"' in bootstrap
    assert 'install -d -o "${VTP_USER}"    -g "${VTP_USER}"    "${VTP_STATE_BASE}/wineprefixes"' in bootstrap
    assert 'install -d -m 700 -o "${VTP_USER}" -g "${VTP_USER}" "${VTP_STATE_BASE}/tmp"' in bootstrap


def test_bootstrap_normalizes_voicetext_shared_runtime_permissions() -> None:
    bootstrap = (REPO_ROOT / "scripts/00-bootstrap.sh").read_text()

    assert 'usermod -aG seasonalweather "${VTP_USER}"' in bootstrap
    assert 'Normalizing VoiceText Paul runtime permissions for seasonalweather + ${VTP_USER}' in bootstrap
    assert 'chown -R seasonalweather:seasonalweather "${VTP_ENGINE_DIR}"' in bootstrap
    assert 'chmod -R u+rwX,g+rwX,o-rwx "${VTP_ENGINE_DIR}"' in bootstrap
    assert 'find "${VTP_ENGINE_DIR}" -type d -exec chmod 2775 {} +' in bootstrap
    assert 'rm -f "${VTP_BIN_DIR}/output.wav" "${VTP_BIN_DIR}/.voicetext_paul.flock"' in bootstrap


def test_bootstrap_does_not_recursively_clobber_voicetext_owned_state() -> None:
    bootstrap = (REPO_ROOT / "scripts/00-bootstrap.sh").read_text()

    assert 'chown -R seasonalweather:seasonalweather /var/lib/seasonalweather /var/log/seasonalweather' not in bootstrap
    assert 'chown seasonalweather:seasonalweather /var/lib/seasonalweather /var/lib/seasonalweather/state /var/lib/seasonalweather/state/cache /var/lib/seasonalweather/jobs /var/lib/seasonalweather/artifacts /var/lib/seasonalweather/artifacts/audio /var/log/seasonalweather' in bootstrap


def test_bootstrap_installs_and_enables_voicetext_xvfb_service() -> None:
    bootstrap = (REPO_ROOT / "scripts/00-bootstrap.sh").read_text()

    assert 'dpkg --add-architecture i386' in bootstrap
    assert 'apt-get install -y --no-install-recommends wine wine32:i386 unzip xvfb x11-utils' in bootstrap
    assert 'wine64' not in bootstrap
    assert 'loginctl enable-linger "${VTP_USER}"' in bootstrap
    assert 'cp /opt/seasonalweather/app/systemd/seasonalweather-voicetext-xvfb.service /etc/systemd/system/seasonalweather-voicetext-xvfb.service' in bootstrap
    assert 'systemctl enable --now seasonalweather-voicetext-xvfb.service' in bootstrap
    assert 'VTP_SYSTEMD_DROPIN="${VTP_SYSTEMD_DROPIN_DIR}/10-voicetext-paul.conf"' in bootstrap
    assert 'After=seasonalweather-voicetext-xvfb.service' in bootstrap


def test_bootstrap_smoke_tests_voicetext_paul_before_service_use() -> None:
    bootstrap = (REPO_ROOT / "scripts/00-bootstrap.sh").read_text()

    assert 'SEASONAL_VOICETEXT_PAUL_SOURCE' in bootstrap
    assert 'SEASONAL_VOICETEXT_PAUL_SHA256' in bootstrap
    assert 'SEASONAL_VOICETEXT_PAUL_SMOKE:-1' in bootstrap
    assert 'VoiceText Paul smoke test failed; the backend is not safe to enable yet' in bootstrap
    assert 'wrapper retries crash rc=134/139 after wineserver -k' in bootstrap
    assert 'runuser -u "${VTP_USER}" -- env' in bootstrap
    assert 'HOME="${VTP_HOME}"' in bootstrap
    assert 'VOICETEXT_PAUL_ATTEMPTS="${VOICETEXT_PAUL_ATTEMPTS:-3}"' in bootstrap
    assert 'runuser -u seasonalweather -- rm -f "${VTP_BIN_DIR}/output.wav"' in bootstrap
    assert 'VoiceText Paul smoke test synthesized audio, but seasonalweather could not clean up output.wav' in bootstrap
    assert 'Initializing VoiceText Paul Wine prefix' in bootstrap
    assert 'SEASONAL_VOICETEXT_PAUL_RECREATE_NON_WIN32_PREFIX:-1' in bootstrap
    assert 'Existing VoiceText Paul Wine prefix is ${VTP_PREFIX_ARCH}; recreating it as a 32-bit prefix' in bootstrap
    assert 'wineboot --init' in bootstrap
    assert 'wine_env_exports | runuser -u "${VTP_USER}" -- env' in bootstrap


def test_bootstrap_preserves_git_checkout_in_deployed_app_tree() -> None:
    bootstrap = (REPO_ROOT / "scripts/00-bootstrap.sh").read_text()

    assert 'rsync -a --delete "${SRC_DIR}/" /opt/seasonalweather/app/' in bootstrap
    assert '--exclude ".git"' not in bootstrap
