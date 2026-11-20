ifneq ($(APPLY),)
ifneq ($(APPLY),1)
$(error APPLY must be unset or exactly 1 (got "$(APPLY)"); usage: make review-infra-setup [APPLY=1] [ONLY=NAME,...])
endif
endif

INFRA_CONFIG := .github/review.yml
INFRA_APPLY := $(if $(APPLY),--apply)

export ONLY

define THESEUS
set -eu; \
tmp="$$(mktemp -d)"; \
trap 'rm -rf "$$tmp"' EXIT; \
git show HEAD:src/theseus_reviewer/bootstrap.py > "$$tmp/bootstrap.py"; \
exe="$$(python3 -I -S "$$tmp/bootstrap.py" install --repo . --commit "$$(git rev-parse HEAD)")"
endef

.PHONY: review-infra-status review-infra-setup review-infra-rotate

review-infra-status:
	@$(THESEUS); "$$exe" infra status --config $(INFRA_CONFIG) $${ONLY:+--only "$$ONLY"}

review-infra-setup:
	@$(THESEUS); "$$exe" infra setup --config $(INFRA_CONFIG) $${ONLY:+--only "$$ONLY"} $(INFRA_APPLY)

review-infra-rotate:
	@$(THESEUS); "$$exe" infra rotate --config $(INFRA_CONFIG) $${ONLY:+--only "$$ONLY"} $(INFRA_APPLY)
