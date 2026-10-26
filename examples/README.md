# discount-service (bundled demonstration fixture)

A small, self-contained Python package used by the AI Engineering Change
Auditor's **mandatory end-to-end acceptance test** and its **local
demonstration mode**. It requires no GitLab connection and no credentials.

## Branches

| Branch | Meaning |
| --- | --- |
| `main` | Target branch. Contains the correct `apply_discount` implementation. |
| `feature/refactor-discount` | Source branch of the simulated merge request. Contains a **known, deliberately introduced defect**. |

## The planted defect

`apply_discount(price, discount_rate)` must return the **final price payable**.
Its documented contract states:

> For a price of 1000 and a discount of 20%, the final price must be 800.

The `feature/refactor-discount` branch instead returns the **discount amount**:

```python
return price * discount_rate          # 1000 * 0.20 -> 200   (WRONG)
```

where the contract requires:

```python
return price * (1 - discount_rate)    # 1000 * 0.20 -> 800   (CORRECT)
```

The existing test suite (`tests/test_pricing_existing.py`) does **not** cover
this contract, which is precisely the situation the auditor is built to catch:
a change that looks plausible, passes CI, and silently breaks behaviour.

## Running it yourself

```bash
cd examples/discount-service
git checkout feature/refactor-discount
python -m pytest                      # existing suite: passes (coverage gap)
python -m pytest tests/test_pricing_existing.py -q
python -c "from discount_service.pricing import apply_discount; print(apply_discount(1000, 0.20))"
```
