export type ProviderType = "mock" | "openai_compatible";
export type CredentialSource = "none" | "environment" | "stored";
export type RunStatus = "pending" | "running" | "completed" | "failed" | "cancelled";
export type QuestionType = "exact_match" | "multiple_choice" | "numeric";

export interface ListResponse<T> {
  items: T[];
  total: number;
  offset: number;
  limit: number;
}

export interface ModelConfig {
  id: string;
  name: string;
  provider_type: ProviderType;
  base_url: string | null;
  remote_model_name: string | null;
  credential_source: CredentialSource;
  has_api_key: boolean;
  /** @deprecated Legacy environment-variable reference; never render it as a secret input. */
  api_key_env: string | null;
  enabled: boolean;
  input_price_per_million: number | null;
  output_price_per_million: number | null;
  default_parameters: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ModelPayload {
  name: string;
  provider_type: ProviderType;
  base_url: string | null;
  remote_model_name: string | null;
  /** Write-only. Model reads expose only `has_api_key`. */
  api_key?: string;
  enabled: boolean;
  input_price_per_million: number | null;
  output_price_per_million: number | null;
  default_parameters: Record<string, unknown>;
}

export interface Benchmark {
  id: string;
  slug: string;
  name: string;
  version: string;
  description: string;
  dimension: string;
  language: string;
  license: string;
  source: string;
  evaluator_type: string;
  evaluator_config: Record<string, unknown>;
  prompt_template: Record<string, unknown>;
  schema_version: string;
  dataset_hash: string;
  question_count: number;
  is_demo: boolean;
  created_at: string;
}

export interface EvaluationRun {
  id: string;
  model_id: string;
  benchmark_id: string;
  status: RunStatus;
  protocol_version: string;
  model_parameters_snapshot: Record<string, unknown>;
  benchmark_hash_snapshot: string;
  prompt_template_snapshot: Record<string, unknown>;
  code_commit_sha: string | null;
  total_questions: number;
  completed_questions: number;
  correct_questions: number;
  error_questions: number;
  score: number | null;
  completion_rate: number | null;
  answered_accuracy: number | null;
  average_latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost: number | null;
  cancellation_requested: boolean;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  error_message: string | null;
}

export interface RunPayload {
  model_id: string;
  benchmark_id: string;
  temperature: number;
  top_p: number;
  max_tokens: number | null;
  seed: number | null;
  system_prompt?: string | null;
  concurrency: number;
  read_timeout_seconds: number;
}

export interface EvaluationResponse {
  id: string;
  run_id: string;
  question_id: string;
  question_external_id: string;
  question_type: QuestionType;
  prompt: string;
  choices: Record<string, string> | null;
  raw_response: string | null;
  parsed_answer: unknown;
  reference_answer_snapshot: unknown;
  score: number;
  evaluator_name: string;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost: number | null;
  error_type: string | null;
  error_message: string | null;
  created_at: string;
}

export interface LeaderboardEntry {
  run_id: string;
  model_id: string;
  model_name: string;
  benchmark_id: string;
  benchmark_slug: string;
  benchmark_name: string;
  benchmark_version: string;
  benchmark_hash: string;
  is_demo: boolean;
  protocol_version: string;
  score: number;
  answered_accuracy: number | null;
  completion_rate: number;
  average_latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost: number | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface DashboardSummary {
  model_count: number;
  benchmark_count: number;
  run_count: number;
  completed_run_count: number;
  failed_run_count: number;
  average_score: number | null;
  average_latency_ms: number | null;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  total_estimated_cost: number | null;
  recent_runs: LeaderboardEntry[];
}
