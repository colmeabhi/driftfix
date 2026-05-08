import { useState } from 'react';

interface Step2Props {
  tenantId: string;
  cfnYaml: string;
  cloudtrailBucket: string;
  onNext: () => void;
  onBack: () => void;
}

export default function Step2Deploy({ tenantId, cfnYaml, cloudtrailBucket, onNext, onBack }: Step2Props) {
  const [copied, setCopied] = useState(false);
  const [showYaml, setShowYaml] = useState(false);
  const [checked, setChecked] = useState(false);

  function copyYaml() {
    navigator.clipboard.writeText(cfnYaml);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  function downloadYaml() {
    const blob = new Blob([cfnYaml], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `drift-detector-setup-${tenantId}.yaml`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="max-w-2xl mx-auto">
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-gray-900">Deploy the CloudFormation template</h2>
        <p className="mt-1.5 text-gray-500 text-sm">
          Deploy this template in your AWS account <code className="bg-gray-100 px-1 py-0.5 rounded text-gray-600 font-mono">{tenantId}</code>.
          It creates all required resources in one step.
        </p>
      </div>

      {/* Download / Copy buttons */}
      <div className="flex gap-3 mb-6">
        <button
          onClick={downloadYaml}
          className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold px-4 py-2.5 rounded-lg text-sm transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
          </svg>
          Download template
        </button>
        <button
          onClick={copyYaml}
          className="flex items-center gap-2 bg-white hover:bg-gray-50 text-gray-700 font-semibold px-4 py-2.5 rounded-lg text-sm border border-gray-300 transition-colors"
        >
          {copied ? (
            <>
              <svg className="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
              </svg>
              Copied!
            </>
          ) : (
            <>
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
              Copy YAML
            </>
          )}
        </button>
        <button
          onClick={() => setShowYaml(v => !v)}
          className="flex items-center gap-2 bg-white hover:bg-gray-50 text-gray-500 font-medium px-4 py-2.5 rounded-lg text-sm border border-gray-200 transition-colors"
        >
          {showYaml ? 'Hide' : 'Preview'} YAML
        </button>
      </div>

      {/* YAML preview */}
      {showYaml && (
        <div className="mb-6 rounded-xl overflow-hidden border border-gray-200">
          <div className="bg-gray-800 px-4 py-2 flex items-center justify-between">
            <span className="text-xs text-gray-400 font-mono">drift-detector-setup-{tenantId}.yaml</span>
          </div>
          <pre className="bg-gray-900 text-gray-200 text-xs p-4 overflow-auto max-h-64 leading-relaxed">
            {cfnYaml}
          </pre>
        </div>
      )}

      {/* Step-by-step deploy instructions */}
      <div className="bg-white rounded-xl border border-gray-200 p-5 mb-6">
        <h3 className="font-semibold text-gray-800 text-sm mb-4">How to deploy</h3>
        <ol className="space-y-4">
          {[
            {
              step: 1,
              title: 'Open AWS CloudFormation',
              body: (
                <>
                  Go to{' '}
                  <a
                    href="https://console.aws.amazon.com/cloudformation/home#/stacks/create/template"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-indigo-600 hover:underline font-medium"
                  >
                    AWS Console → CloudFormation → Create Stack
                  </a>{' '}
                  and make sure you're in account <code className="bg-gray-100 px-1 py-0.5 rounded font-mono text-gray-600">{tenantId}</code>.
                </>
              ),
            },
            {
              step: 2,
              title: 'Upload or paste the template',
              body: 'Select "Upload a template file" and upload the downloaded YAML, or choose "Amazon S3 URL" if you prefer to paste.',
            },
            {
              step: 3,
              title: 'Name the stack',
              body: (
                <>
                  Give it any name, e.g.{' '}
                  <code className="bg-gray-100 px-1 py-0.5 rounded font-mono text-gray-600">drift-detector-setup</code>
                </>
              ),
            },
            {
              step: 4,
              title: 'Acknowledge IAM capabilities and deploy',
              body: 'On the final review page, check the IAM acknowledgment box and click "Create stack". Wait for status to show CREATE_COMPLETE (usually 1–2 minutes).',
            },
          ].map(({ step, title, body }) => (
            <li key={step} className="flex gap-3">
              <div className="w-6 h-6 rounded-full bg-indigo-100 text-indigo-600 flex items-center justify-center text-xs font-bold shrink-0 mt-0.5">
                {step}
              </div>
              <div>
                <p className="text-sm font-medium text-gray-800">{title}</p>
                <p className="text-sm text-gray-500 mt-0.5">{body}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>

      {/* What gets created summary */}
      <div className="bg-gray-50 rounded-xl border border-gray-100 p-4 mb-6 text-sm">
        <p className="font-medium text-gray-700 mb-2">Resources created in your account:</p>
        <ul className="space-y-1 text-gray-500">
          <li>• <strong className="text-gray-700">S3 Bucket</strong>: <code className="text-xs bg-white border border-gray-200 px-1 py-0.5 rounded font-mono">{cloudtrailBucket}</code></li>
          <li>• <strong className="text-gray-700">CloudTrail Trail</strong>: <code className="text-xs bg-white border border-gray-200 px-1 py-0.5 rounded font-mono">drift-detector-trail</code></li>
          <li>• <strong className="text-gray-700">IAM Role</strong>: <code className="text-xs bg-white border border-gray-200 px-1 py-0.5 rounded font-mono">drift-detector-cross-account</code></li>
        </ul>
      </div>

      {/* Confirmation checkbox */}
      <label className="flex items-start gap-3 cursor-pointer mb-6 group">
        <input
          type="checkbox"
          checked={checked}
          onChange={e => setChecked(e.target.checked)}
          className="w-4 h-4 mt-0.5 rounded border-gray-300 text-indigo-600 cursor-pointer"
        />
        <span className="text-sm text-gray-600 group-hover:text-gray-800 transition-colors">
          I've deployed the stack and the status shows <strong>CREATE_COMPLETE</strong>
        </span>
      </label>

      <div className="flex gap-3">
        <button
          onClick={onBack}
          className="flex items-center gap-2 text-gray-500 hover:text-gray-700 font-medium px-4 py-2.5 rounded-lg text-sm border border-gray-200 hover:border-gray-300 bg-white transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18" />
          </svg>
          Back
        </button>
        <button
          onClick={onNext}
          disabled={!checked}
          className="flex-1 flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-300 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors"
        >
          Stack is deployed, continue
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
          </svg>
        </button>
      </div>
    </div>
  );
}
