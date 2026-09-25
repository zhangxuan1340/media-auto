// MediaAuto 前端 — app.js
// 入口: 页面加载后启动
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载

initTheme();   // 夜间模式: 首屏已由 <head> 内联脚本应用, 这里启动"按时间自动切换"定时器
boot();
