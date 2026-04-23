Hi, my tech lead Sarah just assigned me this ticket. Can you help me work through it?

---

**[${REPO_NAME}] Iteration: Architecture cleanup + feature enhancements**

**Assignee**: Kevin Zhang
**Reporter**: Sarah Mitchell

---

**Description**

Hey Kevin, I did a quick review of the ${REPO_NAME} codebase last week and put together some improvement ideas. The code is in your local checkout — take a look and pick up what you think makes sense for this iteration.

Here are my notes:

${FEATURE_PLAN}

---

OK so here's my setup — the repo is already cloned at ${WORK_DIR}/repo and that's my current working directory. I run tests with `docker run --rm -v ${WORK_DIR}/repo:/repo -v ${WORK_DIR}:${WORK_DIR} -w /repo ${DEPS_IMAGE} python3 -m pytest -x --timeout=60`. I use host-side tools (`cat`, `git`, `find`) for browsing code, but all execution goes through the container. No `pip install` on the host — deps are already in the image.

Let's start with the refactoring items first, then move on to features. Run tests after each change. If a test breaks, try to fix it — but don't spend too long debugging. If it's still failing after a couple of attempts, just commit what you have and move on to the next item. Sarah will follow up.

Go ahead and start exploring the code, then work through Sarah's directions.
