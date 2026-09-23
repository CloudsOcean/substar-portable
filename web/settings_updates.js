(() => {
  const panel = document.querySelector('[data-update-section]');
  const status = panel.querySelector('[data-update-status]');
  const notes = panel.querySelector('[data-update-notes]');
  const check = panel.querySelector('[data-update-check]');
  const download = panel.querySelector('[data-update-download]');
  const install = panel.querySelector('[data-update-install]');
  let timer = null, instance = null;
  async function api(path, post = false) {
    if (post && !instance) instance = (await (await fetch('/api/runtime/identity')).json()).instance_id;
    const response = await fetch('/api/updates/' + path, {method:post ? 'POST' : 'GET',
      cache:'no-store', headers:post ? {'X-Substar-Instance-Id':instance} : {}});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '更新请求失败');
    return result;
  }
  async function refresh() {
    clearTimeout(timer);
    try {
      const value = await api('status');
      panel.querySelector('[data-update-version]').textContent = '当前版本 ' + value.current_version;
      install.hidden = value.status !== 'ready';
      install.disabled = !value.supported;
      download.disabled = value.status === 'downloading';
      check.disabled = ['downloading','installing'].includes(value.status);
      if (value.status === 'downloading') {
        status.textContent = `正在下载：${(value.downloaded / 1048576).toFixed(1)} MB`;
        timer = setTimeout(refresh,1500);
      } else if (value.status === 'ready') status.textContent = value.supported
        ? '更新包已校验。请保存其他窗口中的编辑，再重启更新。'
        : '更新包已校验；当前是源码运行环境，请手动安装便携版。';
      else if (value.status === 'failed') { status.textContent = value.error; download.disabled = false; }
      else if (value.last_result?.status === 'rolled_back') status.textContent = '上次更新未完成，已恢复旧版本：' + value.last_result.error;
      else if (value.last_result?.status === 'succeeded') status.textContent = '已更新至 ' + value.last_result.version;
      else if (!value.supported) status.textContent = '当前为源码运行环境，可检查更新；自动安装仅用于正式便携版。';
    } catch (error) { status.textContent = error.message; }
  }
  check.onclick = async () => {
    check.disabled = true; status.textContent = '正在检查更新…';
    try {
      const value = await api('check');
      notes.textContent = value.notes;
      status.textContent = value.available ? `发现新版本 ${value.version}` : '当前已经是最新正式版';
      download.hidden = !value.available;
    } catch(error) { status.textContent = error.message; }
    finally { check.disabled = false; }
  };
  download.onclick = async () => {
    download.disabled = true;
    try { await api('download',true); await refresh(); }
    catch(error) { status.textContent = error.message; download.disabled = false; }
  };
  install.onclick = async () => {
    install.disabled = true;
    try {
      if (typeof saveAllBeforeNavigation === 'function') await saveAllBeforeNavigation();
      await api('install',true);
      status.textContent = '正在退出并更新，完成后会重新打开 Substar。';
    } catch(error) { status.textContent = error.message; install.disabled = false; }
  };
  document.querySelector('[data-panel="general"]').addEventListener('click',refresh);
  refresh();
})();
