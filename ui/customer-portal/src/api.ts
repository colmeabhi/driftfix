export interface InitResponse {
  external_id: string;
  cfn_yaml: string;
  cloudtrail_bucket: string;
  role_arn: string;
  already_exists: boolean;
  status: string;
}

export interface CompleteResponse {
  success: boolean;
  tenant_id: string;
  github_repo: string;
  message: string;
}

const BASE = import.meta.env.DEV ? '' : '';

async function request<T>(path: string, body: object): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? 'Unknown error');
  }

  return res.json();
}

export function initTenant(tenantId: string): Promise<InitResponse> {
  return request<InitResponse>('/customer/init', { tenant_id: tenantId });
}

export function completeTenant(
  tenantId: string,
  githubRepo: string,
  githubPat: string,
): Promise<CompleteResponse> {
  return request<CompleteResponse>('/customer/complete', {
    tenant_id: tenantId,
    github_repo: githubRepo,
    github_pat: githubPat,
  });
}
