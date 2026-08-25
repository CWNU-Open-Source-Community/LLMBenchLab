import type { RunStatus } from "../api/types";
import { statusLabels } from "../lib/format";

export function StatusBadge({ status }: { status: RunStatus }) {
  return <span className={`status-badge status-${status}`}><i />{statusLabels[status]}</span>;
}
