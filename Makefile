INFRA_CONFIG := .github/review.yml
INFRA_ONLY := $(if $(ONLY),--only $(ONLY))
INFRA_APPLY := $(if $(APPLY),--apply)
THESEUS = "$$(python3 -I -S src/theseus_reviewer/bootstrap.py install --repo . --commit "$$(git rev-parse HEAD)")"

.PHONY: review-infra-status review-infra-setup review-infra-rotate

review-infra-status:
	$(THESEUS) infra status --config $(INFRA_CONFIG) $(INFRA_ONLY)

review-infra-setup:
	$(THESEUS) infra setup --config $(INFRA_CONFIG) $(INFRA_ONLY) $(INFRA_APPLY)

review-infra-rotate:
	$(THESEUS) infra rotate --config $(INFRA_CONFIG) $(INFRA_ONLY) $(INFRA_APPLY)
