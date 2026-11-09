# driftfix

driftfix finds manual changes to AWS infrastructure and opens a pull request that updates the CloudFormation template to match.

<p align="center">
  <img src="docs/diagrams/flow.svg" alt="Six steps from a manual console change to a pull request" width="100%">
  <br><em>From a manual change to a pull request in six steps.</em>
</p>

Built by Abhishek Naresh Ahirrao, Kunal Elchipuram, and Ethan Machado. The project was first called the IaC Drift Reconciliation SaaS.

## Contents

- [The problem](#the-problem)
- [What driftfix does](#what-driftfix-does)
- [How it works](#how-it-works)
- [Customer onboarding](#customer-onboarding)
- [Test results](#test-results)
- [Monitoring](#monitoring)
- [Run it yourself](#run-it-yourself)
- [Known limitations](#known-limitations)
- [Reference](#reference)
- [Team](#team)

---

## The problem

Infrastructure as Code works on one assumption: the template in Git describes what is running in the cloud.

That assumption breaks when someone changes a resource by hand. Picture a production incident late at night. An engineer opens the AWS Console and resizes an instance from `t3.large` to `t3.xlarge`. The incident ends, and nobody updates the template.

<p align="center">
  <img src="docs/diagrams/drift.svg" alt="A template in Git and the running resource match, then a console change makes them disagree" width="100%">
  <br><em>Git still says t3.large. AWS now runs t3.xlarge.</em>
</p>

This mismatch is called **drift**. It causes no trouble until the next deploy. CloudFormation then reads the template, sees `t3.large`, and resizes the instance back down. The original problem returns, and the reason is hard to trace.

AWS can report that a stack has drifted. It does not write the fix. Someone still has to find what changed, edit the right file, and open a pull request. driftfix automates that work.

## What driftfix does

driftfix runs once a day for each customer and does three things:

1. **Detect.** It reads the customer's CloudTrail logs and finds manual changes to resources that CloudFormation manages.
2. **Fix.** It sends the template and the CloudTrail events to an LLM on Amazon Bedrock, which returns an updated template.
3. **Propose.** It validates the result with cfn-lint and opens a pull request that explains the change.

driftfix never changes infrastructure and never merges anything. A person reviews each pull request and decides what to do.

### Example pull request

For the resize described above, the team that owns the stack would find this pull request the next morning:

> **[Drift] ModifyInstanceAttribute detected on web**
>
> | Field | Value |
> |---|---|
> | Stack | `web` |
> | Event | `ModifyInstanceAttribute` |
> | Time | 2026-01-10T07:00:00Z |
> | Actor | `arn:aws:iam::111111111111:user/alice` |
> | Model confidence | 82% |
>
> **What changed:** Instance resized to t3.xlarge.
>
> **Root cause:** Manual resize in the console.
>
> **Files updated:** `stacks/web/prod.json`

If the resize was correct, the team merges the pull request and Git matches AWS again. If it was a mistake, they close it and redeploy. In both cases the change is now visible.

---

## How it works

### Architecture

driftfix is multi-tenant. It runs in its own AWS account and reads from each customer's account through one read-only IAM role.

<p align="center">
  <img src="docs/diagrams/architecture.svg" alt="driftfix reads CloudTrail logs from the customer account through a read-only role, runs four Lambda stages, and opens a pull request on GitHub" width="100%">
  <br><em>One manual change moving from the customer's account to a pull request.</em>
</p>

Three rules keep customers isolated from each other:

| Rule | How it works |
|---|---|
| Short-lived credentials | Each run calls STS `AssumeRole` and receives credentials that last one hour and work in one customer account |
| One `ExternalId` per customer | The role can only be assumed with a secret value created at onboarding, so one customer cannot point driftfix at another customer's account |
| Tokens in Secrets Manager | GitHub tokens are read at runtime and never written to logs or to the database |

### The four stages

The pipeline is four Lambda functions. Each one does a single job and then invokes the next.

| Stage | Job | Details |
|---|---|---|
| 1. Orchestrator | Find manual changes | Reads the last 72 hours of CloudTrail logs and keeps successful write calls. Skips changes made by CloudFormation itself, so a normal deploy is not treated as drift. Uses the `aws:cloudformation:stack-name` tag to find the stack that owns each resource |
| 2. Stack Processor | Write the fix | Runs once per affected stack, in parallel. Fetches the template and its parameter files from GitHub and sends them to Bedrock with the CloudTrail events |
| 3. Validator | Check the fix | Runs cfn-lint on the model's output |
| 4. PR Creator | Propose the fix | Commits all changed files in one commit on a `drift/...` branch and opens the pull request |

**Why parameter files are included.** The value that drifted is often not in the template. Suppose `template.yaml` contains `InstanceType: !Ref Size` and `prod.json` sets `Size` to `t3.large`. The correct fix is one line in `prod.json`. A model that sees only the template would write `t3.xlarge` directly into it, which would change every other environment too. driftfix sends both files, and the model returns only the files it changed.

### Retrying when the template is invalid

An LLM sometimes returns a template that is not valid CloudFormation. driftfix handles this as a normal case.

<p align="center">
  <img src="docs/diagrams/retry.svg" alt="The Validator rejects the first template, sends the lint errors back, and the second attempt passes" width="100%">
  <br><em>The first attempt fails cfn-lint. The errors go back to the model, and the second attempt passes.</em>
</p>

When cfn-lint reports errors, the Validator sends the template back to the Stack Processor with the list of errors. The model receives its previous answer and the errors, and produces a corrected template.

The loop runs at most three times. If the template is still invalid, the events are marked `failed`, the invocation goes to a dead-letter queue, and an alarm fires.

Only cfn-lint errors block a template. Warnings are logged and ignored, because many hand-written templates already contain warnings that the model did not introduce.

### Using the model's confidence

The model returns more than a template. It also returns a summary, a root cause, a confidence score from 0 to 1, and a flag that asks for manual review. driftfix uses these to decide what to do:

| Model output | Result |
|---|---|
| A change with confidence of 0.6 or higher | A normal pull request that includes the model's explanation |
| A change flagged for manual review, or with confidence below 0.6 | A pull request marked `needs review` in its title, with a warning banner that lists the reasons |
| No change, because the evidence is not sufficient | No pull request. The reason is saved with the event |

The threshold is set by the `REVIEW_CONFIDENCE_THRESHOLD` environment variable on the Stack Processor.

### Tracking every event

A daily batch job can fail in two ways: it can process the same event twice, or it can lose an event that failed. driftfix prevents both by keeping one record per CloudTrail event in DynamoDB.

<p align="center">
  <img src="docs/diagrams/lifecycle.svg" alt="An event moves from queued to pr_opened, no_change or failed, and failed events are retried on the next run" width="100%">
  <br><em>Three events with three outcomes. The failed event is retried on the next run and succeeds.</em>
</p>

| Status | Meaning | On the next run |
|---|---|---|
| `queued` | A run has picked up the event | Skipped, unless it has been queued for more than 2 hours |
| `pr_opened` | A pull request contains the fix | Skipped |
| `no_change` | The model found nothing to change | Skipped |
| `failed` | A stage reported an error | Retried, up to 3 attempts in total |

Failed events are retried on later runs, which is why the Orchestrator reads 72 hours of logs while running every 24 hours.

These rules are defined in [`lambdas/shared/reconciliation.py`](lambdas/shared/reconciliation.py). A diagram of the complete system and a step-by-step description of every stage are in [`docs/system-architecture.md`](docs/system-architecture.md).

---

## Customer onboarding

Customers sign up through a web portal without help from the driftfix team.

<p align="center">
  <img src="docs/diagrams/onboarding.svg" alt="Customer onboarding in four steps" width="100%">
  <br><em>The four onboarding steps.</em>
</p>

The template in step 2 creates what driftfix needs in the customer's account: a CloudTrail trail that records write events, an S3 bucket for the logs, and the read-only role. The same resources are available as Terraform in [`terraform/customer/`](terraform/customer/).

The portal is a React front end ([`ui/customer-portal/`](ui/customer-portal/)) served by a FastAPI backend ([`admin_ui/app.py`](admin_ui/app.py)). It is packaged with Docker and hosted on AWS App Runner. The course deployment ran at `https://fxcbu33sqc.us-east-1.awsapprunner.com/portal/`. The guide for customers is [`docs/customer-onboarding.md`](docs/customer-onboarding.md).

---

## Test results

### Load tests

On 2026-05-04 we ran five scenarios against a real AWS account with 10 CloudFormation stacks. A script created real drift by calling AWS APIs such as `PutBucketEncryption` and `ModifyDBInstance`, and we then started the Orchestrator manually.

| Scenario | Load | Question |
|---|---|---|
| T1 Functional | 1 stack | Does the pipeline work end to end? |
| T2 Scaling | 3 stacks | Do stacks run in parallel? |
| T3 Performance | 10 stacks | How fast is it at peak load? |
| T4 Failure | 10 stacks and 1 broken tenant | Does one bad customer block the others? |
| T5 Security | 6 stacks | Do any secrets appear in the logs? |

**Lambda scaled with the load.** Concurrency matched the number of drifted stacks in every scenario, from 1 to 10, with no throttles and no errors.

<p align="center">
  <img src="docs/diagrams/throughput.svg" alt="Throughput rises from 15.9 stacks per minute with 1 drifted stack to 39.5 with 10" width="100%">
  <br><em>Throughput for each scenario, ordered by load.</em>
</p>

**The bottleneck is the LLM.** The median Bedrock call (P50) stayed between 4 and 5.5 seconds at every load above one stack. The slowest 1% of calls (P99) grew from 1.7 seconds with one stack to between 13.3 and 14.9 seconds with ten, because parallel requests wait for model capacity.

<p align="center">
  <img src="docs/diagrams/latency.svg" alt="Bedrock P50 latency stays near 5 seconds while P99 latency climbs to about 14 seconds at 10 parallel stacks" width="100%">
  <br><em>Bedrock latency as the number of parallel stacks increases.</em>
</p>

**A broken tenant did not affect the others.** In T4 we added a tenant with an invalid IAM role. The Orchestrator logged the error and continued, and all 10 valid stacks finished normally.

**No secrets appeared in the logs.** In T5 we searched every Lambda log group for `AKIA`, `ghp_`, `password`, `secret_key`, and `aws_secret`, and found no matches.

The full report and the raw CloudWatch exports are in [`results/report.md`](results/report.md).

### Failure tests

Three scripts in [`tests/failure_scenarios/`](tests/failure_scenarios/) each cause one type of failure and check that the monitoring detects it.

| Scenario | What we did | Expected signal | Dead-letter queue |
|---|---|---|---|
| Poison pill | Sent the Validator a template with four cfn-lint violations | 3 Lambda errors and the `drift-detector-validator-errors` alarm | 1 message |
| Throttle | Sent 40 invocations against a concurrency limit of 10 | About 30 throttles and the `drift-detector-validator-throttled` alarm | About 10 messages |
| Degraded zone | Reduced the Validator's memory to slow it down, which imitates a struggling availability zone | Higher P99 duration with no errors | Empty |

Each scenario is described in [`tests/failure_scenarios/README.md`](tests/failure_scenarios/README.md).

### Unit tests

The decision logic in the pipeline has unit tests that run without an AWS account. They cover which events count as drift, when an event is retried, how the model's reply is parsed, the cfn-lint retry loop, and the content of the pull request.

```bash
pip install boto3 pytest
```

```bash
pytest tests/unit
```

---

## Monitoring

The pipeline runs once a day, so a failure could go unnoticed for many hours. A separate Health Check Lambda runs every 5 minutes. It checks each Lambda function, both DynamoDB tables, the audit bucket, and the dead-letter queue, and publishes a `drift-detector/HealthCheck` metric for each one. A value of `1` means healthy, and `0` triggers an alarm.

| Dashboard | Shows |
|---|---|
| `drift-detector-status` | Whether every component is healthy right now |
| `drift-detector-overview` | Invocations, errors, P95 and P99 duration, throttles, cold starts, and concurrency |
| `drift-detector-cost` | Estimated Lambda cost for the selected time range |

There are 17 alarms: 8 for component health and 9 for pipeline behaviour such as errors, throttles, timeouts, and dead-letter queue depth. All alarms send notifications through SNS. More detail is in [`docs/observability.md`](docs/observability.md).

---

## Run it yourself

You need an AWS account with access to a Bedrock model, Terraform 1.5 or newer, and an S3 bucket for the Lambda zip files.

1. Package the Lambdas and upload them:

   ```bash
   ./scripts/package_lambdas.sh <your-lambda-zip-bucket>
   ```

2. Copy `terraform/saas/terraform.tfvars.example` to `terraform.tfvars` and fill in the values.

3. Deploy:

   ```bash
   cd terraform/saas && terraform init && terraform apply
   ```

The four pipeline Lambdas invoke each other, so deploy them together.

The diagrams and charts in this README are generated by a script. To change them, edit the script and run it again:

```bash
python docs/diagrams/generate.py
```

---

## Known limitations

driftfix started as a course project. These parts are not ready for production:

| Limitation | Effect |
|---|---|
| The portal has no login | Anyone who can reach the portal can list, add, and remove tenants. It needs authentication before it is exposed to the internet |
| Templates outside GitHub | If the template is not in the customer's repository, driftfix uses the copy stored by CloudFormation and proposes it as `template.yaml` in the repository root, which may be the wrong location |
| Tenants are processed in sequence | All tenants share one 15-minute Orchestrator run, which limits how many customers a run can serve |
| No CI | The unit tests are run manually |

---

## Reference

<details>
<summary>AWS services used</summary>

| Service | Purpose |
|---|---|
| AWS Lambda | The four pipeline functions and the health check |
| Amazon Bedrock | The LLM that rewrites templates. The default model is Nova Lite |
| AWS CloudTrail | The log of every API call in the customer's account |
| Amazon DynamoDB | `drift-detector-tenants` stores customer configuration. `drift-detector-reconciliations` stores one record per event |
| Amazon S3 | CloudTrail logs in the customer's account and validated templates in the driftfix account |
| Amazon EventBridge | The daily trigger, `cron(0 7 * * ? *)`, and the 5-minute health check schedule |
| Amazon SQS | The dead-letter queue for failed invocations |
| Amazon CloudWatch and SNS | Dashboards, alarms, metric filters, and notifications |
| AWS Secrets Manager | GitHub tokens |
| AWS STS and IAM | Cross-account access with temporary credentials, and one role per Lambda |
| AWS App Runner and Amazon ECR | Hosting and the image registry for the customer portal |
| Terraform | Defines all of the above in [`terraform/`](terraform/) |

</details>

<details>
<summary>Repository layout</summary>

```
.
├── lambdas/
│   ├── processor/          Orchestrator: reads CloudTrail, finds drift, fans out per stack
│   ├── stack_processor/    Fetches the template, calls Bedrock, decides what to do with the answer
│   ├── validator/          Runs cfn-lint and drives the retry loop
│   ├── pr_creator/         Commits the changed files and opens the pull request
│   ├── health_check/       Probes every component every 5 minutes
│   └── shared/             Event status lifecycle, packaged into every Lambda
├── admin_ui/               FastAPI backend for the customer portal
├── ui/customer-portal/     React + Vite + Tailwind front end
├── terraform/
│   ├── saas/               driftfix's own infrastructure, dashboards and alarms
│   └── customer/           What a customer deploys: CloudTrail, S3, cross-account role
├── tests/
│   ├── unit/               Unit tests, no AWS needed
│   ├── e2e.py              End-to-end test against a deployed pipeline
│   └── failure_scenarios/  The three break-it-on-purpose scripts
├── scripts/
│   ├── package_lambdas.sh  Builds and uploads the Lambda zips
│   └── load_test/          Load test runner and graph generation
├── results/                Load test report, graphs G1 to G7, raw metric exports
├── docs/                   Architecture, onboarding and observability guides
│   └── diagrams/           README diagrams and charts, and the script that draws them
├── Dockerfile              Portal image: React build + FastAPI
└── .env.example
```

</details>

---

## Team

Built by Abhishek Naresh Ahirrao, Kunal Elchipuram, and Ethan Machado.

<details>
<summary>Who did what</summary>

### Abhishek Naresh Ahirrao

- Designed the end-to-end multi-tenant SaaS architecture with idempotency and tenant isolation as core constraints.
- Designed the four-stage asynchronous Lambda pipeline, EventBridge scheduling, DynamoDB deduplication, S3 handoff, and SQS DLQ flow.
- Authored and deployed the Terraform configuration covering Lambda, IAM, DynamoDB, SQS, CloudWatch, and EventBridge.
- Created architecture diagrams illustrating the two-account topology, pipeline sequence, and concurrency fan-out.
- Wrote the architecture, scaling, and cost analysis sections of the technical report.
- Designed cross-account IAM using STS AssumeRole with ExternalId protection and per-invocation session renewal.

### Kunal Elchipuram

- Designed and implemented the customer-facing onboarding portal frontend, including the onboarding forms and guided customer setup flow.
- Updated the customer onboarding CloudFormation template to support self-service tenant onboarding.
- Dockerized the customer portal application and deployed it through Amazon ECR and AWS App Runner.
- Executed the five structured load test scenarios covering functional correctness, scaling, failure isolation, security, and performance.
- Collected, analyzed, and presented the performance metrics used in the report and presentation, including latency, throughput, concurrency, and resource utilization.
- Experimented with and evaluated different Amazon Bedrock models for deployment suitability.
- Configured the Bedrock integration in the Stack Processor Lambda for LLM-based CloudFormation template generation.
- Helped write the related components in the report and presentation slides.

### Ethan Machado

- Created the example customer repository, including the CI/CD pipeline and CloudFormation test stacks.
- Created and executed the dedicated failure scenarios.
- Created three CloudWatch dashboards for service status, operations overview, and cost visibility.
- Implemented CloudWatch alarms and the SNS email notification topic.
- Implemented recurring health checks for major pipeline components.
- Implemented write-only management log filtering on the customer tenant side.
- Created and collaborated on several presentation slides.
- Presented the introduction, customer-side system demo, and failure scenario sections.
- Helped write the Abstract, Problem Definition, Motivation, Related Work, Implementation Details, Failure Modeling, Architectural Trade-Off Analysis, and Conclusions sections of the final report.

</details>
