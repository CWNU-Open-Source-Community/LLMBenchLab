import { FlaskConical, Gauge, History, Layers3, Play, Trophy } from "lucide-react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";

import { BenchmarksPage } from "./pages/BenchmarksPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LeaderboardPage } from "./pages/LeaderboardPage";
import { ModelsPage } from "./pages/ModelsPage";
import { NewRunPage } from "./pages/NewRunPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { RunDetailPage } from "./pages/RunDetailPage";
import { RunsPage } from "./pages/RunsPage";

const navItems = [
  { label: "概览", icon: Gauge, to: "/" },
  { label: "模型", icon: Layers3, to: "/models" },
  { label: "评测集", icon: FlaskConical, to: "/benchmarks" },
  { label: "评测记录", icon: History, to: "/runs" },
  { label: "排行榜", icon: Trophy, to: "/leaderboard" },
];

const pageNames: Record<string, string> = {
  "/": "概览",
  "/models": "模型注册表",
  "/benchmarks": "Benchmark 数据集",
  "/runs": "评测记录",
  "/runs/new": "新建评测",
  "/leaderboard": "排行榜",
};

function App() {
  const location = useLocation();
  const normalizedPath = location.pathname.replace(/\/+$/, "") || "/";
  const pageName = normalizedPath.startsWith("/runs/") && normalizedPath !== "/runs/new"
    ? "Run 详情"
    : pageNames[normalizedPath] || "LLMBenchLab";
  return <div className="app-shell">
    <aside className="sidebar">
      <Link className="brand" to="/" aria-label="LLMBenchLab 首页"><span className="brand-mark">LB</span><span><strong>LLMBench</strong><em>Lab</em></span></Link>
      <nav aria-label="主导航">{navItems.map(({ label, icon: Icon, to }) => <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => isActive ? "nav-item active" : "nav-item"}><Icon size={18} strokeWidth={1.8} />{label}</NavLink>)}</nav>
      <Link className="sidebar-run-button" to="/runs/new"><Play size={15} fill="currentColor" /> 新建评测</Link>
      <div className="protocol-card"><span>当前协议</span><strong>protocol-v1</strong><small>结果按数据版本隔离比较</small></div>
    </aside>
    <main className="main-content">
      <header className="topbar"><div><span className="eyebrow">PERSONAL MODEL EVALUATION</span><strong>{pageName}</strong></div><span className="health"><i /> 本地服务</span></header>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/models" element={<ModelsPage />} />
        <Route path="/benchmarks" element={<BenchmarksPage />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/runs/new" element={<NewRunPage />} />
        <Route path="/runs/:runId" element={<RunDetailPage />} />
        <Route path="/leaderboard" element={<LeaderboardPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </main>
  </div>;
}

export default App;
