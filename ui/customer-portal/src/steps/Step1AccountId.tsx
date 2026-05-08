import { useState } from 'react';
import { initTenant } from '../api';
import type { InitResponse } from '../api';

interface Step1Props {
  onNext: (data: InitResponse & { tenantId: string }) => void;
}

export default function Step1AccountId({ onNext }: Step1Props) {
  const [accountId, setAccountId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const valid = /^\d{12}$/.test(accountId.trim());

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setLoading(true);
    setError('');
    try {
      const data = await initTenant(accountId.trim());
      onNext({ ...data, tenantId: accountId.trim() });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Something went wrong. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="max-w-lg mx-auto">
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-gray-900">Connect your AWS account</h2>
        <p className="mt-1.5 text-gray-500 text-sm">
          We'll generate a CloudFormation template that sets up the required resources in your account.
          No manual configuration needed.
        </p>
      </div>

      {/* What gets created */}
      <div className="bg-indigo-50 border border-indigo-100 rounded-xl p-4 mb-6">
        <p className="text-xs font-semibold text-indigo-700 uppercase tracking-wide mb-3">
          What the template creates in your account
        </p>
        <ul className="space-y-2">
          {[
            ['S3 Bucket', 'Stores CloudTrail log files, auto-expires after 90 days'],
            ['CloudTrail Trail', 'Records all management write events across regions'],
            ['Cross-Account IAM Role', 'Read-only access so Drift Detector can scan logs'],
          ].map(([title, desc]) => (
            <li key={title} className="flex items-start gap-2.5">
              <svg className="w-4 h-4 text-indigo-500 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
              </svg>
              <span className="text-sm text-indigo-800">
                <strong className="font-medium">{title}</strong>
                <span className="text-indigo-600"> — {desc}</span>
              </span>
            </li>
          ))}
        </ul>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1.5">
            AWS Account ID
          </label>
          <input
            type="text"
            value={accountId}
            onChange={e => setAccountId(e.target.value.replace(/\D/g, '').slice(0, 12))}
            placeholder="123456789012"
            maxLength={12}
            className={`w-full px-3.5 py-2.5 rounded-lg border text-sm font-mono transition-colors outline-none
              ${accountId && !valid
                ? 'border-red-300 bg-red-50 focus:border-red-400 focus:ring-2 focus:ring-red-100'
                : 'border-gray-300 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100'
              }`}
          />
          <p className="mt-1.5 text-xs text-gray-400">
            Find this in the top-right corner of your AWS console or run{' '}
            <code className="bg-gray-100 px-1 py-0.5 rounded text-gray-600">aws sts get-caller-identity</code>
          </p>
          {accountId && !valid && (
            <p className="mt-1 text-xs text-red-500">Must be exactly 12 digits</p>
          )}
        </div>

        {error && (
          <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-lg p-3">
            <svg className="w-4 h-4 text-red-500 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
            <p className="text-sm text-red-700">{error}</p>
          </div>
        )}

        <button
          type="submit"
          disabled={!valid || loading}
          className="w-full flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-300 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors"
        >
          {loading ? (
            <>
              <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Generating template…
            </>
          ) : (
            <>
              Generate CloudFormation template
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
              </svg>
            </>
          )}
        </button>
      </form>
    </div>
  );
}
