def print_gugudan(dan: int) -> None:
    """지정한 단의 구구단을 출력한다."""
    print(f"\n[{dan}단]")
    for number in range(1, 10):
        print(f"{dan} x {number} = {dan * number}")


def print_all_gugudan() -> None:
    """2단부터 9단까지 모든 구구단을 출력한다."""
    for dan in range(2, 10):
        print_gugudan(dan)


def main() -> None:
    user_input = input(
        "출력할 단을 입력하세요(2~9). 전체를 보려면 Enter를 누르세요: "
    ).strip()

    if user_input == "":
        print_all_gugudan()
        return

    try:
        dan = int(user_input)
    except ValueError:
        print("숫자 2~9 중 하나를 입력해 주세요.")
        return

    if 2 <= dan <= 9:
        print_gugudan(dan)
    else:
        print("2단부터 9단까지만 출력할 수 있습니다.")


if __name__ == "__main__":
    main()
