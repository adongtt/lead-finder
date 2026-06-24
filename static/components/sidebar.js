(function () {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;

  const active = sidebar.dataset.active || '';
  const showChangelog = sidebar.dataset.changelog === 'true';

  const items = [
    { id: 'index',     icon: '🔍', label: '搜索',       href: '/' },
    { id: 'history',   icon: '📁', label: '历史结果',   href: '/history' },
    { id: 'dashboard', icon: '📊', label: '数据看板',   href: '/dashboard' },
    { id: 'contacts',  icon: '📇', label: '已联系客户', href: '/contacts' },
    { id: 'templates', icon: '🚀', label: '搜索模板',   href: '/templates' },
    { id: 'tools',     icon: '🛠️', label: '批量工具',   href: '/tools' },
    { id: 'settings',  icon: '⚙️', label: '系统状态',   href: '/settings' },
    { id: 'feedback',  icon: '💬', label: '问题反馈',   href: '/feedback' },
    { id: 'guide',     icon: '📖', label: '使用手册',   href: '/static/USER_GUIDE.html' },
  ];

  let html = '<h2>B2B 客户开发</h2>';
  items.forEach(it => {
    const cls = it.id === active ? 'nav-item active' : 'nav-item';
    html += `<div class="${cls}" onclick="location.href='${it.href}'"><span>${it.icon}</span><span>${it.label}</span></div>`;
  });

  if (showChangelog) {
    html += `
      <div class="changelog-section">
        <h3>更新日志</h3>
        <div class="changelog-list">
          <div class="changelog-item">
            <div class="changelog-date">2026-06-24</div>
            <div><strong>左侧导航升级</strong>：新增已联系客户、搜索模板、批量工具、系统状态、问题反馈入口。</div>
          </div>
          <div class="changelog-item">
            <div class="changelog-date">2026-06-23</div>
            <div><strong>Apollo 搜索质量优化</strong>：默认关联度阈值提升至 0；扩展负向行业词库；增加关键词二次校验；新增最小关联度滑块与严格模式开关。</div>
          </div>
          <div class="changelog-item">
            <div class="changelog-date">2026-06-23</div>
            <div><strong>修复 Apollo "无邮箱" 问题</strong>：默认过滤 has_email=True 但未暴露真实地址的联系人；增加 Hunter domain-search 兜底。</div>
          </div>
          <div class="changelog-item">
            <div class="changelog-date">2026-06-23</div>
            <div><strong>修复公司域名解析</strong>：过滤 LeadiQ、RocketReach、Seamless.AI 等数据平台及新闻站点。</div>
          </div>
          <div class="changelog-item">
            <div class="changelog-date">2026-06-22</div>
            <div><strong>Google Maps 搜索优化</strong>：自动过滤已关门商家、增加关键词相关度评分、支持 TLD 筛选与结果数量上限。</div>
          </div>
          <div class="changelog-item">
            <div class="changelog-date">2026-06-16</div>
            <div><strong>新增左侧导航栏与历史结果页面</strong>；搜索记录支持多维度筛选，管理员可查看所有人记录。</div>
          </div>
          <div class="changelog-item">
            <div class="changelog-date">2026-06-16</div>
            <div><strong>启用供应商门户模式</strong>：批量扫描域名采购/供应商页面，提取邮箱、表单链接与摘要。</div>
          </div>
        </div>
      </div>
    `;
  }

  html += '<div class="nav-item logout" onclick="logout()"><span>🚪</span><span>退出</span></div>';
  sidebar.innerHTML = html;
})();
