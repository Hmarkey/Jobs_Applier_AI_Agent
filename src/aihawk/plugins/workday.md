---
name: workday
hosts: [myworkdayjobs.com, myworkdaysite.com]
apply_path: /apply
---
A Workday form needs a candidate account on the company's own site: every company is a separate account, and the browser profile keeps each sign-in. The pages are one web app, and the address barely changes as it moves. Controls are best found by their `data-automation-id`, as `[data-automation-id="email"]`; the `id` attributes are numbered and change.

The page at `/apply` is "Start Your Application" with `applyManually` ("Apply Manually"), `autofillWithResume`, `useMyLastApplication` and a LinkedIn frame: click `applyManually`. Do not use "Autofill with Resume", "Sign in with Google", "Sign in with LinkedIn" or the LinkedIn frame: the account is made with the candidate's email, never through another service.

With Simplify Copilot (when the simplify_fill tool is offered), the account is Simplify's to make: after `applyManually`, the "Create Account" page shows the add-on's "Create Account & Autofill" button, and simplify_fill presses it and the "Create Account" that follows; the account is made with the candidate's email and a password Simplify keeps, Workday signs in, and the add-on fills "My Information" by itself. Do the account by hand, as below, only when simplify_fill says there is no panel or the page did not move on. Then call simplify_fill again on every later step, before filling anything: it fills the step and attaches the resume on "My Experience".

Sign in or create the account by hand. The step "Create Account/Sign In" shows `SignInWithEmailButton` ("Sign in with email"): click it. The sign-in form has `email`, `password`, `signInSubmitButton` ("Sign In"), `createAccountLink` ("Create Account") and `forgotPasswordLink`. The brief says whether an account of yours is known on this site. Sign in with the candidate's email and the `account_password` from the answers, or click `createAccountLink` where there is none yet. The create form has `email`, `password`, `verifyPassword`, the checkbox `createAccountCheckbox` ("I agree", the terms and the privacy notice of the company) and `createAccountSubmitButton` ("Create Account"). Use the `account_password` from the answers, the same on every company, so the candidate can sign in again; if the answers have none, call finish_form with status blocked and say so.

Both forms carry a hidden text field `beecatcher` (name `website`, "This input is for robots only"): never type in it.

A code sent by email. After "Create Account", or at sign-in, the site may ask to verify the email address with a code it sent. Workday's messages are expected from an address at `myworkday.com`, often `<company>@myworkday.com`, though no real Workday mail has been read: read it as the mail instructions say, then come back to the form's address, read the page again and type the code where it asks.

If sign-in with the account_password fails and the site says the account already exists, call finish_form with status blocked and say the account needs a password the answers do not hold. Do not try another password: Workday locks an account after a few attempts, and that account is the person's only way back to this company.

The steps after sign-in, shown in the progress bar: "My Information", "My Experience", "Application Questions", "Voluntary Disclosures", "Self Identify" on some sites, and "Review". Each step ends with `[data-automation-id="bottom-navigation-next-button"]` ("Save and Continue"), and the page lists its errors at the top in red when a required field is missing: read them and fix each field before clicking again.

My Information: "How Did You Hear About Us?" is a list and "Have you previously worked for ..." a yes or no. Then the legal name (`legalNameSection_firstName`, `legalNameSection_lastName`), the address (`addressSection_addressLine1`, `addressSection_city`, `addressSection_postalCode`, with a region list on most countries) and the phone (`phone-device-type`, a country code list, `phone-number`).

My Experience: work experience, education and skills are blocks added with "Add" buttons. Where Simplify attached the resume (its file name shows in the "Resume/CV" section), leave it. Otherwise upload the CV with the file input in the "Resume/CV" section, `[data-automation-id="file-upload-input-ref"]` or the `input[type="file"]` there, and wait until the file name shows. Websites and social links are optional.

Application Questions: the company's own questions, mostly lists and yes or no, all usually required. Voluntary Disclosures and Self Identify: demographic questions with a "prefer not to say" or "I do not wish to answer" option, and a "Terms and Conditions" checkbox that is required.

Review shows every answer; the send control is `[data-automation-id="bottom-navigation-next-button"]` there, labelled "Submit". Some sites name that button `[data-automation-id="pageFooterNextButton"]` instead, on every step. Review is the end: do not go back from it to check or change the earlier steps (each one was checked by Workday when you saved it). Call finish_form on Review at once, with its Submit control.

After sending: the address leaves `/apply` for `/jobTasks/completed/application`, and the page says the application was submitted and offers the candidate home or more jobs. The Review page can stay on screen for some seconds after the click, its "Submit" button included: seen on a real form, where it was read as a refusal just before the address moved on. Only errors shown at the top mean the application was not received.
