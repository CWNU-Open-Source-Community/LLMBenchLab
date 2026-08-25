import { AlertTriangle, Inbox, LoaderCircle, RotateCcw } from "lucide-react";

export function LoadingState({ label = "正在读取数据" }: { label?: string }) {
  return <div className="state-card"><LoaderCircle className="spin" size={22} /><strong>{label}</strong><span>请稍候，正在连接本地服务。</span></div>;
}

export function ErrorState({ message, retry }: { message: string; retry?: () => void }) {
  return <div className="state-card error-state"><AlertTriangle size={22} /><strong>暂时无法完成请求</strong><span>{message}</span>{retry && <button className="secondary-button" onClick={retry}><RotateCcw size={15} /> 重试</button>}</div>;
}

export function EmptyState({ title, message, action }: { title: string; message: string; action?: React.ReactNode }) {
  return <div className="state-card"><Inbox size={22} /><strong>{title}</strong><span>{message}</span>{action}</div>;
}
