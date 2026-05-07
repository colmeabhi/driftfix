interface StepDoneProps {
  tenantId: string;
  githubRepo: string;
}

function nextScanTime(): string {
  const now = new Date();
  const next = new Date();
  next.setUTCHours(7, 0, 0, 0);
  if (next <= now) next.setUTCDate(next.getUTCDate() + 1);
  return next.toLocaleString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  });
}

export default function StepDone({ tenantId, githubRepo }: StepDoneProps) {
  return (
    <div className="max-w-lg mx-auto text-center py-4">
      {/* Success animation */}
      <div className="inline-flex items-center justify-center w-20 h-20 rounded-full bg-green-100 mb-6">
        <svg className="w-10 h-10 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      </div>

      <h2 className="text-2xl font-bold text-gray-900 mb-2">You're all set!</h2>
      <p className="text-gray-500 text-sm mb-8 leading-relaxed">
        Drift Detector is now monitoring your AWS infrastructure. When drift is detected, you'll
        receive a pull request in your repository automatically.
      </p>

      {/* Summary card */}
      <div className="bg-gray-50 rounded-xl border border-gray-100 p-5 text-left mb-8">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-4">
          Your account summary
        </h3>
        <dl className="space-y-3">
          {[
            { label: 'AWS Account ID', value: tenantId },
            { label: 'GitHub Repository', value: githubRepo },
            { label: 'CloudTrail Bucket', value: `drift-detector-cloudtrail-${tenantId}` },
            { label: 'IAM Role', value: `drift-detector-cross-account` },
            { label: 'First scan', value: nextScanTime() },
          ].map(({ label, value }) => (
            <div key={label} className="flex justify-between gap-4 text-sm">
              <dt className="text-gray-500 shrink-0">{label}</dt>
              <dd className="font-mono text-gray-800 text-right truncate text-xs bg-white border border-gray-200 px-2 py-1 rounded">
                {value}
              </dd>
            </div>
          ))}
        </dl>
      </div>

      {/* What happens next */}
      <div className="bg-white rounded-xl border border-gray-200 p-5 text-left mb-8">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-4">
          What happens next
        </h3>
        <ul className="space-y-3">
          {[
            {
              icon: (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
              ),
              color: 'text-blue-500 bg-blue-50',
              text: 'Daily at 7 AM UTC, Drift Detector scans your CloudTrail logs for changes.',
            },
            {
              icon: (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
              ),
              color: 'text-purple-500 bg-purple-50',
              text: 'If a resource drifted from your IaC, an AI-generated fix is prepared.',
            },
            {
              icon: (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M17.25 6.75L22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3l-4.5 16.5" />
              ),
              color: 'text-green-500 bg-green-50',
              text: `A pull request is opened in ${githubRepo} with the reconciliation fix.`,
            },
          ].map((item, i) => (
            <li key={i} className="flex items-start gap-3">
              <div className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 ${item.color}`}>
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  {item.icon}
                </svg>
              </div>
              <p className="text-sm text-gray-600 leading-relaxed pt-0.5">{item.text}</p>
            </li>
          ))}
        </ul>
      </div>

      <p className="text-xs text-gray-400">
        Questions? Contact{' '}
        <a href="mailto:support@drift-detector.io" className="text-indigo-600 hover:underline">
          support@drift-detector.io
        </a>
      </p>
    </div>
  );
}
