import { ArrowLeft } from "lucide-react";
import { Link } from "react-router-dom";

export function NotFoundPage() {
  return <div className="not-found"><span>404</span><h1>这里没有评测证据</h1><p>链接可能不完整，或页面尚未纳入当前 MVP。</p><Link className="primary-button" to="/"><ArrowLeft size={15} /> 返回概览</Link></div>;
}
