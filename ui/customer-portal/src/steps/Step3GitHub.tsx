import { useState } from 'react';
import { completeTenant } from '../api';

interface Step3Props {
  tenantId: string;
  onNext: (githubRepo: string) => void;
  onBack: () => void;
}

export default function Step3GitHub({ tenantId, onNext, onBack }: Step3Props) {
  const [githubRepo, setGithubRepo] = useState('');
  const [githubPat, setGithubPat] = useState('');
  const [showPat, setShowPat] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const repoValid = /^[\w.-]+\/[\w.-]+$/.test(githubRepo.trim());
  const patValid  = githubPat.trim().length > 10;
  const canSubmit = repoValid && patValid;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setLoading(true);
    setError('');
    try {
      await completeTenant(tenantId, githubRepo.trim(), githubPat.trim());
      onNext(githubRepo.trim());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Something went wrong. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="max-w-lg mx-auto">
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-gray-900">Connect your GitHub repository</h2>
        <p className="mt-1.5 text-gray-500 text-sm">
          Drift Detector will open pull requests in your IaC repository when drift is detected.
          We need read + PR write access to do this.
        </p>
      </div>

      {/* Validation notice */}
      <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 mb-6">
        <div className="flex gap-2.5">
          <svg className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />
          </svg>
          <p className="text-sm text-amber-700">
            Before clicking "Complete Setup", make sure the CloudFormation stack status shows{' '}
            <strong>CREATE_COMPLETE</strong>. We'll verify the cross-account role is accessible.
          </p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-5">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1.5">
            GitHub Repository
          </label>
          <div className="relative">
            <div className="absolute inset-y-0 left-3 flex items-center pointer-events-none">
              <svg className="w-4 h-4 text-gray-400" fill="currentColor" viewBox="0 0 24 24">
                <path d="M12 0C5.374 0 0 5.373 0 12c0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23A11.509 11.509 0 0112 5.803c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576C20.566 21.797 24 17.3 24 12c0-6.627-5.373-12-12-12z" />
              </svg>
            </div>
            <input
              type="text"
              value={githubRepo}
              onChange={e => setGithubRepo(e.target.value)}
              placeholder="acme-corp/infrastructure"
              className={`w-full pl-9 pr-3.5 py-2.5 rounded-lg border text-sm transition-colors outline-none
                ${githubRepo && !repoValid
                  ? 'border-red-300 bg-red-50 focus:border-red-400 focus:ring-2 focus:ring-red-100'
                  : 'border-gray-300 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100'
                }`}
            />
          </div>
          <p className="mt-1.5 text-xs text-gray-400">Format: <code className="bg-gray-100 px-1 py-0.5 rounded">owner/repository-name</code></p>
          {githubRepo && !repoValid && (
            <p className="mt-1 text-xs text-red-500">Use the format owner/repo (e.g. acme/infra)</p>
          )}
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1.5">
            GitHub Personal Access Token
          </label>
          <div className="relative">
            <input
              type={showPat ? 'text' : 'password'}
              value={githubPat}
              onChange={e => setGithubPat(e.target.value)}
              placeholder="github_pat_..."
              className="w-full pr-10 px-3.5 py-2.5 rounded-lg border border-gray-300 text-sm focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 outline-none transition-colors font-mono"
            />
            <button
              type="button"
              onClick={() => setShowPat(v => !v)}
              className="absolute inset-y-0 right-3 flex items-center text-gray-400 hover:text-gray-600"
            >
              {showPat ? (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3.98 8.223A10.477 10.477 0 001.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.45 10.45 0 0112 4.5c4.756 0 8.773 3.162 10.065 7.498a10.523 10.523 0 01-4.293 5.774M6.228 6.228L3 3m3.228 3.228l3.65 3.65m7.894 7.894L21 21m-3.228-3.228l-3.65-3.65m0 0a3 3 0 10-4.243-4.243m4.242 4.242L9.88 9.88" />
                </svg>
              ) : (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z" />
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
              )}
            </button>
          </div>
          <div className="mt-2 p-3 bg-gray-50 rounded-lg border border-gray-100">
            <p className="text-xs font-medium text-gray-600 mb-1.5">Required PAT permissions:</p>
            <ul className="text-xs text-gray-500 space-y-0.5">
              <li>• <strong>Contents</strong>: Read and write (to read templates)</li>
              <li>• <strong>Pull requests</strong>: Read and write (to open fix PRs)</li>
            </ul>
            <a
              href="https://github.com/settings/tokens?type=beta"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 mt-2 text-xs text-indigo-600 hover:underline"
            >
              Create a fine-grained PAT on GitHub
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
              </svg>
            </a>
          </div>
        </div>

        {error && (
          <div className="flex items-start gap-2.5 bg-red-50 border border-red-200 rounded-lg p-3">
            <svg className="w-4 h-4 text-red-500 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
            <p className="text-sm text-red-700">{error}</p>
          </div>
        )}

        <div className="flex gap-3 pt-1">
          <button
            type="button"
            onClick={onBack}
            className="flex items-center gap-2 text-gray-500 hover:text-gray-700 font-medium px-4 py-2.5 rounded-lg text-sm border border-gray-200 hover:border-gray-300 bg-white transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18" />
            </svg>
            Back
          </button>
          <button
            type="submit"
            disabled={!canSubmit || loading}
            className="flex-1 flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-300 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors"
          >
            {loading ? (
              <>
                <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                Verifying &amp; activating…
              </>
            ) : (
              <>
                Complete Setup
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </>
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
