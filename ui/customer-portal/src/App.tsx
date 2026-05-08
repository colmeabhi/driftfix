import { useState, useEffect } from 'react';
import Stepper from './components/Stepper';
import Landing from './steps/Landing';
import Step1AccountId from './steps/Step1AccountId';
import Step2Deploy from './steps/Step2Deploy';
import Step3GitHub from './steps/Step3GitHub';
import StepDone from './steps/StepDone';
import type { InitResponse } from './api';

type View = 'landing' | 'step1' | 'step2' | 'step3' | 'done';

interface OnboardingData {
  tenantId: string;
  externalId: string;
  cfnYaml: string;
  cloudtrailBucket: string;
  roleArn: string;
  githubRepo: string;
}

const STORAGE_KEY = 'drift-detector-onboarding';

const WIZARD_STEPS = [
  { label: 'AWS Account' },
  { label: 'Deploy' },
  { label: 'GitHub' },
  { label: 'Done' },
];

const VIEW_TO_STEP_IDX: Record<View, number> = {
  landing: -1,
  step1: 0,
  step2: 1,
  step3: 2,
  done: 3,
};

export default function App() {
  const [view, setView] = useState<View>('landing');
  const [data, setData] = useState<Partial<OnboardingData>>({});

  // Restore progress from localStorage
  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved) as { view: View; data: Partial<OnboardingData> };
        if (parsed.view && parsed.view !== 'landing') {
          setView(parsed.view);
          setData(parsed.data ?? {});
        }
      }
    } catch {
      // ignore stale/invalid storage
    }
  }, []);

  function persist(nextView: View, nextData: Partial<OnboardingData>) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ view: nextView, data: nextData }));
    } catch {
      // ignore storage errors
    }
  }

  function go(nextView: View, nextData?: Partial<OnboardingData>) {
    const merged = { ...data, ...nextData };
    setView(nextView);
    setData(merged);
    persist(nextView, merged);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function reset() {
    localStorage.removeItem(STORAGE_KEY);
    setView('landing');
    setData({});
  }

  function handleInit(res: InitResponse & { tenantId: string }) {
    go('step2', {
      tenantId: res.tenantId,
      externalId: res.external_id,
      cfnYaml: res.cfn_yaml,
      cloudtrailBucket: res.cloudtrail_bucket,
      roleArn: res.role_arn,
    });
  }

  const stepIdx = VIEW_TO_STEP_IDX[view];

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 via-white to-indigo-50 font-sans">
      {/* Header */}
      <header className="border-b border-gray-200 bg-white/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center justify-between">
          <button
            onClick={reset}
            className="flex items-center gap-2.5 hover:opacity-80 transition-opacity"
          >
            <div className="w-7 h-7 rounded-lg bg-indigo-600 flex items-center justify-center">
              <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
              </svg>
            </div>
            <span className="font-semibold text-gray-800 text-sm">Drift Detector</span>
          </button>
          {view !== 'landing' && view !== 'done' && (
            <button
              onClick={reset}
              className="text-xs text-gray-400 hover:text-gray-600 transition-colors"
            >
              Start over
            </button>
          )}
        </div>
      </header>

      {/* Main content */}
      <main className="max-w-4xl mx-auto px-6 py-10">
        {view === 'landing' ? (
          <Landing onStart={() => go('step1')} />
        ) : (
          <>
            {/* Stepper — shown during wizard steps */}
            {view !== 'done' && (
              <Stepper steps={WIZARD_STEPS} currentStep={stepIdx} />
            )}

            {/* Step panels */}
            <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-8">
              {view === 'step1' && (
                <Step1AccountId onNext={handleInit} />
              )}
              {view === 'step2' && (
                <Step2Deploy
                  tenantId={data.tenantId ?? ''}
                  cfnYaml={data.cfnYaml ?? ''}
                  cloudtrailBucket={data.cloudtrailBucket ?? ''}
                  onNext={() => go('step3')}
                  onBack={() => go('step1')}
                />
              )}
              {view === 'step3' && (
                <Step3GitHub
                  tenantId={data.tenantId ?? ''}
                  onNext={(repo) => go('done', { githubRepo: repo })}
                  onBack={() => go('step2')}
                />
              )}
              {view === 'done' && (
                <StepDone
                  tenantId={data.tenantId ?? ''}
                  githubRepo={data.githubRepo ?? ''}
                />
              )}
            </div>
          </>
        )}
      </main>

      {/* Footer */}
      <footer className="text-center py-8 text-xs text-gray-400">
        Drift Detector &copy; {new Date().getFullYear()}
      </footer>
    </div>
  );
}
